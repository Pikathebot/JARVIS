using System.Numerics;
using Microsoft.UI.Xaml;

namespace Jarvis_GlassLab;

/// <summary>
/// Phase 7: everything from Phases 1-6 (transparent window, D3D11/DirectComposition swapchain,
/// live capture, squircle-bezel displacement + refraction + specular rim, resize/display
/// reactivity), now with a real WinUI3 control hosted via a second, owner-linked window stacked
/// above the glass HWND. See the GlassLab plan doc.
/// </summary>
public partial class App : Application
{
    private GlassWindow? _window;
    private D3D11Context? _d3d;
    private CompositionContext? _composition;
    private LiveCaptureSource? _capture;
    private GlassRenderer? _renderer;
    private ControlOverlayWindow? _overlay;
    private Vector4 _uvRect = new(0, 0, 1, 1);
    private string _logPath = "";
    private bool _captureReady;

    public App()
    {
        InitializeComponent();
    }

    protected override void OnLaunched(LaunchActivatedEventArgs args)
    {
        _logPath = Path.Combine(AppContext.BaseDirectory, "glasslab-phase7.log");
        LiveCaptureSource.DiagnosticLogPath = _logPath;
        try
        {
            _window = new GlassWindow();
            _window.Create();

            _d3d = new D3D11Context(900, 600);
            _composition = new CompositionContext(_d3d.DxgiDevice, _window.HandleValue, _d3d.SwapChain);
            _capture = new LiveCaptureSource(_d3d);
            _capture.FirstFrameReceived += () =>
                File.AppendAllText(_logPath, $"[{DateTimeOffset.Now:O}] FIRST FRAME RECEIVED - capture is flowing\n");

            var shaderDir = Path.Combine(AppContext.BaseDirectory, "Shaders");
            _renderer = new GlassRenderer(_d3d.Device, _d3d.ImmediateContext, shaderDir);

            _overlay = new ControlOverlayWindow(_window.HandleValue);
            SyncOverlayBounds();
            _overlay.Activate();

            _window.OnRenderTick = RenderTick;
            _window.OnWindowPosChanged = OnWindowPosChanged;
            _window.OnDisplayChanged = OnDisplayChanged;

            // Fire-and-forget: RequestAccessAsync's continuation resumes on this same thread's
            // DispatcherQueue once RunMessageLoop below starts pumping.
            _ = StartCaptureAsync();

            File.WriteAllText(_logPath, $"[{DateTimeOffset.Now:O}] Phase 7 window+swapchain+renderer+overlay ready, hwnd={_window.HandleValue:X}\n");
            _window.RunMessageLoop();
        }
        catch (Exception ex)
        {
            File.AppendAllText(_logPath, $"[{DateTimeOffset.Now:O}] Phase 7 FAILED\n{ex}\n");
        }
        finally
        {
            _window?.StopRenderTimer();
            _overlay?.Close();
            _capture?.Dispose();
            _renderer?.Dispose();
            _composition?.Dispose();
            _d3d?.Dispose();
            Exit();
        }
    }

    private void SyncOverlayBounds()
    {
        if (_window is null || _overlay is null)
        {
            return;
        }

        var rect = _window.GetScreenRect();
        _overlay.SyncBounds(rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top);
    }

    private async Task StartCaptureAsync()
    {
        var ok = await _capture!.StartAsync(_window!.Handle);
        if (!ok)
        {
            File.AppendAllText(_logPath, $"[{DateTimeOffset.Now:O}] Live capture FAILED to start (consent denied, or API unavailable)\n");
            return;
        }

        _captureReady = true;
        RecomputeUvRect();

        // Matches Jarvis.Glass's LiveCaptureService throttle - revisited properly in Phase 8.
        _window.StartRenderTimer(1000 / 12);
    }

    /// <summary>
    /// Fires on every WM_WINDOWPOSCHANGED - a pure move, a resize, or both at once (dragging a
    /// window across a monitor boundary is usually both, since Windows adjusts position as part
    /// of the same operation on some DPI transitions). Handles all three: resize the swapchain if
    /// the size actually changed, retarget capture if the display changed, and always recompute
    /// the crop UV rect since the window's position relative to whichever display it's on may
    /// have changed even without a size change.
    /// </summary>
    private void OnWindowPosChanged()
    {
        if (_window is null || _d3d is null || _composition is null)
        {
            return;
        }

        var rect = _window.GetScreenRect();
        var newWidth = rect.right - rect.left;
        var newHeight = rect.bottom - rect.top;

        if (newWidth != _d3d.Width || newHeight != _d3d.Height)
        {
            File.AppendAllText(_logPath,
                $"[{DateTimeOffset.Now:O}] Resizing swapchain {_d3d.Width}x{_d3d.Height} -> {newWidth}x{newHeight}\n");
            _composition.AroundResize(() => _d3d.ResizeBuffers(newWidth, newHeight));

            _capture?.RetargetIfDisplayChanged(_window.HandleValue);
            RecomputeUvRect();

            // Prime both swapchain buffers with fresh content immediately, rather than waiting
            // for the next ~83ms WM_TIMER tick. With BufferCount=2, a single Present after
            // ResizeBuffers only refreshes one of the two buffers - if another resize (or a
            // screenshot) arrives before the timer gets a second tick in, the other buffer can
            // still show stale content from before the resize. Rendering twice here cycles
            // through both buffers right away.
            RenderTick();
            RenderTick();
            SyncOverlayBounds();
            return;
        }

        _capture?.RetargetIfDisplayChanged(_window.HandleValue);
        RecomputeUvRect();
        SyncOverlayBounds();
    }

    private void OnDisplayChanged()
    {
        // A monitor was added/removed/reconfigured - the window may not have moved at all, but
        // the display it's on could still need retargeting (e.g. the monitor it was on was
        // removed and Windows snapped it onto another one without a normal move notification).
        if (_window is null)
        {
            return;
        }

        _capture?.RetargetIfDisplayChanged(_window.HandleValue);
        RecomputeUvRect();
        SyncOverlayBounds();
    }

    private void RecomputeUvRect()
    {
        if (_window is null || _capture is null || !_captureReady)
        {
            return;
        }

        var rect = _window.GetScreenRect();
        var itemSize = _capture.ItemSize;
        if (itemSize.Width <= 0 || itemSize.Height <= 0)
        {
            return;
        }

        _uvRect = new Vector4(
            (float)rect.left / itemSize.Width,
            (float)rect.top / itemSize.Height,
            (float)(rect.right - rect.left) / itemSize.Width,
            (float)(rect.bottom - rect.top) / itemSize.Height);

        File.AppendAllText(_logPath,
            $"[{DateTimeOffset.Now:O}] uvRect recomputed: item={itemSize.Width}x{itemSize.Height} " +
            $"windowRect=({rect.left},{rect.top},{rect.right},{rect.bottom}) uvRect={_uvRect}\n");
    }

    private int _loggedFrameCount = -1;

    private void RenderTick()
    {
        var srv = _capture?.TryGetCurrentFrameSrv();
        if (srv is null || _renderer is null || _d3d is null)
        {
            return;
        }

        _renderer.Draw(_d3d.RenderTargetView, srv, _uvRect, _d3d.Width, _d3d.Height);
        _d3d.Present();

        var frameCount = _capture!.FrameCount;
        if (frameCount != _loggedFrameCount && (frameCount == 1 || frameCount % 12 == 0))
        {
            _loggedFrameCount = frameCount;
            File.AppendAllText(_logPath, $"[{DateTimeOffset.Now:O}] rendered frameCount={frameCount}\n");
        }
    }
}
