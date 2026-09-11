using Vortice.Direct3D11;
using Windows.Graphics.Capture;
using Windows.Graphics.DirectX;
using Windows.Graphics.DirectX.Direct3D11;
using Windows.Security.Authorization.AppCapabilityAccess;
using Windows.Win32.Foundation;

namespace Jarvis_GlassLab;

/// <summary>
/// Phase 3: whole-display capture via Windows.Graphics.Capture, feeding a raw ID3D11Texture2D
/// instead of a Win2D CanvasBitmap. Reimplements the approach in
/// desktop-winui/Jarvis.Glass/LiveCaptureService.cs (same API shape, throttling concept) against
/// this project's own D3D11Context, since this project has no Win2D to lean on.
///
/// This class's real job in Phase 3 is answering an open question, not just moving pixels: does
/// programmatic capture actually produce frames under this package identity, with the
/// graphicsCaptureProgrammatic manifest capability in place? That has never been confirmed on
/// this machine for any prior implementation.
/// </summary>
internal sealed class LiveCaptureSource : IDisposable
{
    private readonly D3D11Context _d3d;
    private readonly object _gate = new();

    private IDirect3DDevice? _direct3DDevice;
    private GraphicsCaptureItem? _item;
    private Direct3D11CaptureFramePool? _framePool;
    private GraphicsCaptureSession? _session;
    private ID3D11Texture2D? _sharedTexture;
    private ID3D11ShaderResourceView? _sharedSrv;
    private int _frameCount;
    private ulong? _currentDisplayIdValue;

    /// <summary>Fired exactly once, the first time a captured frame is actually received - the
    /// unambiguous answer to this phase's real question, independent of whether rendering/
    /// presenting it afterward also works.</summary>
    public event Action? FirstFrameReceived;

    public LiveCaptureSource(D3D11Context d3d)
    {
        _d3d = d3d;
    }

    /// <summary>Size of the captured item (the whole primary display), needed by the renderer to
    /// compute the UV crop for "just the part of the screen behind our window."</summary>
    public Windows.Graphics.SizeInt32 ItemSize { get; private set; }

    public int FrameCount => Volatile.Read(ref _frameCount);

    /// <summary>Snapshot of the current frame's SRV, or null before the first frame arrives. Safe
    /// to call every render frame; swaps under a lock shared with the capture callback.</summary>
    public ID3D11ShaderResourceView? TryGetCurrentFrameSrv()
    {
        lock (_gate)
        {
            return _sharedSrv;
        }
    }

    public async Task<bool> StartAsync(HWND ownHwnd)
    {
        try
        {
            var status = await GraphicsCaptureAccess.RequestAccessAsync(GraphicsCaptureAccessKind.Programmatic);
            if (status != AppCapabilityAccessStatus.Allowed)
            {
                return false;
            }

            // Must happen before StartCapture, not after - otherwise the first frames could
            // capture our own window before DWM has actually excluded it. Note for future
            // debugging: this also makes the window invisible to GDI CopyFromScreen screenshots,
            // not just our own Direct3D11CaptureFramePool - a real person looking at their actual
            // monitor is unaffected (WDA_EXCLUDEFROMCAPTURE only blocks programmatic capture
            // APIs), but any screenshot-based verification tooling needs this disabled first.
            WindowCaptureExclusion.SetExcluded(ownHwnd, true);

            _direct3DDevice = Direct3D11Interop.CreateDirect3DDeviceFromDXGIDevice(_d3d.DxgiDevice);

            var displayArea = Microsoft.UI.Windowing.DisplayArea.Primary
                ?? throw new InvalidOperationException("No primary display area available for capture.");
            CreateSessionForDisplay(displayArea.DisplayId.Value);
            return true;
        }
        catch
        {
            Dispose();
            WindowCaptureExclusion.SetExcluded(ownHwnd, false);
            return false;
        }
    }

    /// <summary>
    /// Re-checks which display the window is currently on and, if it changed (a cross-monitor
    /// drag), tears down and rebuilds the capture session against the new display -
    /// GraphicsCaptureItem.TryCreateFromDisplayId is per-display, so a session started against
    /// one monitor keeps capturing that monitor forever otherwise, even after the window has
    /// moved to a different one. No-ops if the display hasn't actually changed, or if capture was
    /// never successfully started.
    /// </summary>
    public void RetargetIfDisplayChanged(nint hwndValue)
    {
        if (_direct3DDevice is null || _currentDisplayIdValue is null)
        {
            return; // capture never started (denied consent, unavailable API) - nothing to retarget
        }

        Microsoft.UI.WindowId windowId;
        Microsoft.UI.Windowing.DisplayArea? displayArea;
        try
        {
            windowId = Microsoft.UI.Win32Interop.GetWindowIdFromWindow(hwndValue);
            displayArea = Microsoft.UI.Windowing.DisplayArea.GetFromWindowId(
                windowId, Microsoft.UI.Windowing.DisplayAreaFallback.Nearest);
        }
        catch (Exception ex)
        {
            Diag($"RetargetIfDisplayChanged: failed to resolve current display: {ex}");
            return;
        }

        if (displayArea is null || displayArea.DisplayId.Value == _currentDisplayIdValue)
        {
            return;
        }

        Diag($"Display changed: {_currentDisplayIdValue} -> {displayArea.DisplayId.Value}, rebuilding capture session");

        TeardownSessionOnly();
        try
        {
            CreateSessionForDisplay(displayArea.DisplayId.Value);
        }
        catch (Exception ex)
        {
            Diag($"RetargetIfDisplayChanged: failed to rebuild session on new display: {ex}");
        }
    }

    private void CreateSessionForDisplay(ulong displayIdValue)
    {
        _currentDisplayIdValue = displayIdValue;
        var displayId = new Windows.Graphics.DisplayId { Value = displayIdValue };

        _item = GraphicsCaptureItem.TryCreateFromDisplayId(displayId)
            ?? throw new InvalidOperationException("Windows.Graphics.Capture is unavailable for this display.");
        ItemSize = _item.Size;

        _framePool = Direct3D11CaptureFramePool.CreateFreeThreaded(
            _direct3DDevice!,
            DirectXPixelFormat.B8G8R8A8UIntNormalized,
            2,
            _item.Size);
        _framePool.FrameArrived += OnFrameArrived;

        _session = _framePool.CreateCaptureSession(_item);
#pragma warning disable CA1416 // guarded by try/catch for older WindowsAppSDK, matching Jarvis.Glass's approach
        try { _session.IsBorderRequired = false; } catch { /* older WindowsAppSDK: property absent */ }
        try { _session.IsCursorCaptureEnabled = false; } catch { /* same */ }
#pragma warning restore CA1416
        _session.StartCapture();
    }

    /// <summary>Tears down just the session/frame pool/item, keeping the Direct3D device and
    /// window-exclusion state alone - used when rebuilding for a new display, as opposed to
    /// Dispose()'s full teardown.</summary>
    private void TeardownSessionOnly()
    {
        try { _session?.Dispose(); } catch { /* best-effort */ }
        _session = null;

        if (_framePool is not null)
        {
            try { _framePool.FrameArrived -= OnFrameArrived; } catch { /* best-effort */ }
            try { _framePool.Dispose(); } catch { /* best-effort */ }
            _framePool = null;
        }

        _item = null;
    }

    /// <summary>Set to receive a line for every dropped/failed frame - not wired to per-frame
    /// success, which would spam this at capture's full rate.</summary>
    public static string? DiagnosticLogPath;

    private static void Diag(string message)
    {
        if (DiagnosticLogPath is null) return;
        try { File.AppendAllText(DiagnosticLogPath, $"[{DateTimeOffset.Now:O}] [diag] {message}\n"); } catch { }
    }

    private void OnFrameArrived(Direct3D11CaptureFramePool sender, object args)
    {
        using var frame = sender.TryGetNextFrame();
        if (frame is null)
        {
            return;
        }

        try
        {
            using var capturedTexture = Direct3D11Interop.GetTexture(frame.Surface);
            var desc = capturedTexture.Description;

            lock (_gate)
            {
                EnsureSharedTexture(desc);
                // The capture callback fires on its own thread pool thread while the render loop
                // also uses this same immediate context - both are serialized through _gate since
                // an ID3D11DeviceContext is not free-threaded by default.
                _d3d.ImmediateContext.CopyResource(_sharedTexture!, capturedTexture);
            }

            if (Interlocked.Increment(ref _frameCount) == 1)
            {
                FirstFrameReceived?.Invoke();
            }
        }
        catch (Exception ex)
        {
            Diag($"OnFrameArrived threw: {ex}");
        }
    }

    private void EnsureSharedTexture(Texture2DDescription desc)
    {
        if (_sharedTexture is not null &&
            _sharedTexture.Description.Width == desc.Width &&
            _sharedTexture.Description.Height == desc.Height)
        {
            return;
        }

        _sharedSrv?.Dispose();
        _sharedTexture?.Dispose();

        var textureDesc = new Texture2DDescription
        {
            Width = desc.Width,
            Height = desc.Height,
            MipLevels = 1,
            ArraySize = 1,
            Format = desc.Format,
            SampleDescription = new Vortice.DXGI.SampleDescription(1, 0),
            Usage = ResourceUsage.Default,
            BindFlags = BindFlags.ShaderResource,
        };
        _sharedTexture = _d3d.Device.CreateTexture2D(textureDesc);
        _sharedSrv = _d3d.Device.CreateShaderResourceView(_sharedTexture);
    }

    public void Dispose()
    {
        try { _session?.Dispose(); } catch { /* best-effort */ }
        _session = null;

        if (_framePool is not null)
        {
            try { _framePool.FrameArrived -= OnFrameArrived; } catch { /* best-effort */ }
            try { _framePool.Dispose(); } catch { /* best-effort */ }
            _framePool = null;
        }

        _item = null;
        _direct3DDevice = null;
        _currentDisplayIdValue = null;

        lock (_gate)
        {
            _sharedSrv?.Dispose();
            _sharedSrv = null;
            _sharedTexture?.Dispose();
            _sharedTexture = null;
        }
    }
}
