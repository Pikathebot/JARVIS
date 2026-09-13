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
    private ID3D11Texture2D? _frozenTexture;
    private ID3D11ShaderResourceView? _frozenSrv;
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

    /// <summary>The frozen periodic snapshot's SRV, or null before the first one is captured. This
    /// is what the renderer actually draws -- not the continuously-updating <see cref="_sharedSrv"/>
    /// -- since <see cref="_sharedSrv"/> keeps flowing even while our own windows are visible (and
    /// therefore potentially in the captured scene themselves), while the frozen copy is only ever
    /// refreshed during the brief window in <see cref="RefreshFrozenSnapshot"/> where they're
    /// hidden. Safe to call every render frame; swaps under a lock shared with the capture
    /// callback.</summary>
    public ID3D11ShaderResourceView? TryGetCurrentFrameSrv()
    {
        lock (_gate)
        {
            return _frozenSrv;
        }
    }

    /// <summary>The continuously-updating live frame's SRV -- only safe to render from when our
    /// own windows are excluded from the capture (see App.ExcludeFromCaptureMode), otherwise the
    /// glass would refract its own previous frame.</summary>
    public ID3D11ShaderResourceView? TryGetLiveFrameSrv()
    {
        lock (_gate)
        {
            return _sharedSrv;
        }
    }

    /// <summary>Copies the live capture texture into the frozen snapshot the renderer actually
    /// reads. Callers (App.RefreshSnapshotAsync) are responsible for hiding our own windows and
    /// waiting for at least one fresh frame to arrive first -- this method just does the copy, it
    /// has no idea whether the frame it's copying was captured while hidden or not.</summary>
    public void RefreshFrozenSnapshot()
    {
        lock (_gate)
        {
            if (_sharedTexture is null) return;

            var desc = _sharedTexture.Description;
            if (_frozenTexture is null || _frozenTexture.Description.Width != desc.Width || _frozenTexture.Description.Height != desc.Height)
            {
                _frozenSrv?.Dispose();
                _frozenTexture?.Dispose();
                _frozenTexture = _d3d.Device.CreateTexture2D(new Texture2DDescription
                {
                    Width = desc.Width,
                    Height = desc.Height,
                    MipLevels = 1,
                    ArraySize = 1,
                    Format = desc.Format,
                    SampleDescription = new Vortice.DXGI.SampleDescription(1, 0),
                    Usage = ResourceUsage.Default,
                    BindFlags = BindFlags.ShaderResource,
                });
                _frozenSrv = _d3d.Device.CreateShaderResourceView(_frozenTexture);
            }

            _d3d.ImmediateContext.CopyResource(_frozenTexture, _sharedTexture);
        }
    }

    /// <summary>Diagnostic: reads back a small region of the shared capture texture at an
    /// arbitrary point (capture-pixel coordinates, i.e. already in the whole-display's own space)
    /// and logs its average color. Callers pass the glass window's own screen rect (converted to
    /// capture-pixel space) to answer "is THIS specific region black in the capture, independent
    /// of what's actually on screen there" -- the direct test for whether self-exclusion
    /// (WDA_EXCLUDEFROMCAPTURE on this same window) is painting its own region black.</summary>
    public void LogRegionPixel(int x, int y, string label, bool frozen = false)
    {
        ID3D11Texture2D? snapshot;
        lock (_gate)
        {
            snapshot = frozen ? _frozenTexture : _sharedTexture;
        }
        if (snapshot is null) return;

        try
        {
            const uint size = 8;
            var cx = (uint)Math.Clamp(x, 0, Math.Max(0, (int)snapshot.Description.Width - (int)size));
            var cy = (uint)Math.Clamp(y, 0, Math.Max(0, (int)snapshot.Description.Height - (int)size));

            using var staging = _d3d.Device.CreateTexture2D(new Texture2DDescription
            {
                Width = size,
                Height = size,
                MipLevels = 1,
                ArraySize = 1,
                Format = snapshot.Description.Format,
                SampleDescription = new Vortice.DXGI.SampleDescription(1, 0),
                Usage = ResourceUsage.Staging,
                CPUAccessFlags = CpuAccessFlags.Read,
            });

            Vortice.Mathematics.Box srcBox;
            lock (_gate)
            {
                var source = frozen ? _frozenTexture : _sharedTexture;
                if (source is null) return;
                srcBox = new Vortice.Mathematics.Box((int)cx, (int)cy, 0, (int)(cx + size), (int)(cy + size), 1);
                _d3d.ImmediateContext.CopySubresourceRegion(staging, 0, 0, 0, 0, source, 0, srcBox);
            }

            var mapped = _d3d.ImmediateContext.Map(staging, 0, MapMode.Read);
            try
            {
                unsafe
                {
                    var row = (byte*)mapped.DataPointer;
                    var b = row[0];
                    var g = row[1];
                    var r = row[2];
                    var a = row[3];
                    Diag($"LogRegionPixel[{label}]{(frozen ? " (frozen)" : " (live)")}: at capture-px ({cx},{cy}) (B,G,R,A)=({b},{g},{r},{a})");
                }
            }
            finally
            {
                _d3d.ImmediateContext.Unmap(staging, 0);
            }
        }
        catch (Exception ex)
        {
            Diag($"LogRegionPixel[{label}] threw: {ex}");
        }
    }

    /// <summary>Capture exclusion (WDA_EXCLUDEFROMCAPTURE) for our own windows is applied by App
    /// once the overlay exists too, not here -- excluding only the glass HWND while the opaque
    /// overlay stayed capturable is what produced the "region under the window is black" reading
    /// on 2026-09-11 that was misattributed to the flag itself.</summary>
    public async Task<bool> StartAsync(HWND ownHwnd)
    {
        try
        {
            var status = await GraphicsCaptureAccess.RequestAccessAsync(GraphicsCaptureAccessKind.Programmatic);
            LabLog.Write($"programmatic capture access: {status}");
            if (status != AppCapabilityAccessStatus.Allowed)
            {
                return false;
            }

            // Setting IsBorderRequired = false below is silently ignored unless the app has also
            // been granted borderless capture (manifest capability graphicsCaptureWithoutBorder
            // + this request) -- without it Windows keeps drawing its yellow "this display is
            // being captured" frame around the whole screen for as long as the lab is running.
            try
            {
                var borderless = await GraphicsCaptureAccess.RequestAccessAsync(GraphicsCaptureAccessKind.Borderless);
                LabLog.Write($"borderless capture access: {borderless}");
            }
            catch (Exception ex)
            {
                LabLog.Write($"borderless capture access request threw: {ex.Message}");
            }

            _direct3DDevice = Direct3D11Interop.CreateDirect3DDeviceFromDXGIDevice(_d3d.DxgiDevice);

            var displayArea = Microsoft.UI.Windowing.DisplayArea.Primary
                ?? throw new InvalidOperationException("No primary display area available for capture.");
            CreateSessionForDisplay(displayArea.DisplayId.Value);
            return true;
        }
        catch (Exception ex)
        {
            LabLog.Write($"capture start threw: {ex}");
            Dispose();
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

            var count = Interlocked.Increment(ref _frameCount);
            if (count == 1)
            {
                FirstFrameReceived?.Invoke();
            }

            // Diagnostic: logged on the first frame only -- the Map/Unmap readback stalls the
            // GPU pipeline, so it must stay out of the steady-state path.
            if (count == 1)
            {
                LogCenterPixel(capturedTexture, desc);
            }
        }
        catch (Exception ex)
        {
            Diag($"OnFrameArrived threw: {ex}");
        }
    }

    /// <summary>One-off staging-texture readback of a small region at the captured display's
    /// center, logged as its average color. Cheap relative to the alternative (reading the whole
    /// frame) since it copies only a tiny sub-rectangle before mapping.</summary>
    private void LogCenterPixel(ID3D11Texture2D capturedTexture, Texture2DDescription desc)
    {
        try
        {
            const uint size = 8;
            var cx = desc.Width / 2;
            var cy = desc.Height / 2;

            using var staging = _d3d.Device.CreateTexture2D(new Texture2DDescription
            {
                Width = size,
                Height = size,
                MipLevels = 1,
                ArraySize = 1,
                Format = desc.Format,
                SampleDescription = new Vortice.DXGI.SampleDescription(1, 0),
                Usage = ResourceUsage.Staging,
                CPUAccessFlags = CpuAccessFlags.Read,
            });

            var srcBox = new Vortice.Mathematics.Box((int)cx, (int)cy, 0, (int)(cx + size), (int)(cy + size), 1);
            _d3d.ImmediateContext.CopySubresourceRegion(staging, 0, 0, 0, 0, capturedTexture, 0, srcBox);

            var mapped = _d3d.ImmediateContext.Map(staging, 0, MapMode.Read);
            try
            {
                unsafe
                {
                    var row = (byte*)mapped.DataPointer;
                    // desc.Format is B8G8R8A8UIntNormalized (the pool's requested format) -- BGRA byte order.
                    var b = row[0];
                    var g = row[1];
                    var r = row[2];
                    var a = row[3];
                    Diag($"LogCenterPixel: capture desc={desc.Width}x{desc.Height} fmt={desc.Format} centerPixel(B,G,R,A)=({b},{g},{r},{a})");
                }
            }
            finally
            {
                _d3d.ImmediateContext.Unmap(staging, 0);
            }
        }
        catch (Exception ex)
        {
            Diag($"LogCenterPixel threw: {ex}");
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
            _frozenSrv?.Dispose();
            _frozenSrv = null;
            _frozenTexture?.Dispose();
            _frozenTexture = null;
        }
    }
}
