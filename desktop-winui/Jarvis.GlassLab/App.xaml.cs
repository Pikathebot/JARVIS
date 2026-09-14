using System.Numerics;
using Microsoft.UI.Dispatching;
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
    private DispatcherQueueTimer? _snapshotTimer;
    private bool _snapshotRefreshInFlight;

    /// <summary>Both windows are excluded from capture with WDA_EXCLUDEFROMCAPTURE and the glass
    /// renders from the *live* frame -- verified 2026-09-13 by a pixel readback of the capture
    /// under the window (real backdrop content, not black). The earlier belief (2026-09-11) that
    /// this flag paints the window's region black was wrong: what was black was the opaque,
    /// not-yet-excluded WinUI overlay window on top of the glass. The hide-then-snapshot path
    /// (RefreshSnapshotAsync) is kept behind this switch as a fallback only.</summary>
    private const bool ExcludeFromCaptureMode = true;

    public App()
    {
        InitializeComponent();
    }

    protected override void OnLaunched(LaunchActivatedEventArgs args)
    {
        _logPath = Path.Combine(AppContext.BaseDirectory, "glasslab-phase7.log");
        LiveCaptureSource.DiagnosticLogPath = _logPath;
        LabLog.Path = _logPath;
        File.WriteAllText(_logPath, "");
        try
        {
            _window = new GlassWindow();
            _window.Create();

            _d3d = new D3D11Context(960, 760);
            _composition = new CompositionContext(_d3d.DxgiDevice, _window.HandleValue, _d3d.SwapChain);
            _capture = new LiveCaptureSource(_d3d);
            _capture.FirstFrameReceived += () =>
                File.AppendAllText(_logPath, $"[{DateTimeOffset.Now:O}] FIRST FRAME RECEIVED - capture is flowing\n");

            var shaderDir = Path.Combine(AppContext.BaseDirectory, "Shaders");
            _renderer = new GlassRenderer(_d3d.Device, _d3d.ImmediateContext, shaderDir);

            _overlay = new ControlOverlayWindow(_window.HandleValue, _renderer);
            SyncOverlayBounds();
            _overlay.Activate();

            _window.OnRenderTick = RenderTick;
            _window.OnWindowPosChanged = OnWindowPosChanged;
            _window.OnDisplayChanged = OnDisplayChanged;

            // Fire-and-forget: RequestAccessAsync's continuation resumes on this same thread's
            // DispatcherQueue once RunMessageLoop below starts pumping.
            _ = StartCaptureAsync();

            File.AppendAllText(_logPath, $"[{DateTimeOffset.Now:O}] Phase 7 window+swapchain+renderer+overlay ready, hwnd={_window.HandleValue:X}\n");
            _window.RunMessageLoop();
        }
        catch (Exception ex)
        {
            File.AppendAllText(_logPath, $"[{DateTimeOffset.Now:O}] Phase 7 FAILED\n{ex}\n");
        }
        finally
        {
            _snapshotTimer?.Stop();
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
        try
        {
            await StartCaptureCoreAsync();
        }
        catch (Exception ex)
        {
            File.AppendAllText(_logPath, $"[{DateTimeOffset.Now:O}] StartCaptureAsync FAILED\n{ex}\n");
        }
    }

    private async Task StartCaptureCoreAsync()
    {
        var ok = await _capture!.StartAsync(_window!.Handle);
        if (!ok)
        {
            File.AppendAllText(_logPath, $"[{DateTimeOffset.Now:O}] Live capture FAILED to start (consent denied, or API unavailable)\n");
            return;
        }

        _captureReady = true;
        RecomputeUvRect();

        // Debug: "forcelift.txt" next to the exe presets the lens into its lifted state at launch.
        if (File.Exists(Path.Combine(AppContext.BaseDirectory, "forcelift.txt")))
        {
            GlassToggle.Material.ForceLift = 1f;
            GlassToggle.MaterialChanged();
        }

        // WM_TIMER's floor is ~15.6ms, so this is effectively display rate; capture frames arrive
        // at display rate too, so anything slower here is what reads as a laggy backdrop (the
        // previous 1000/12 did -- it was inherited from Jarvis.Glass's throttle, not chosen).
        _window.StartRenderTimer(16);

        if (ExcludeFromCaptureMode)
        {
            WindowCaptureExclusion.SetExcluded(_window.Handle, true);
            WindowCaptureExclusion.SetExcluded(_overlay!.Handle, true);

            // One-off proof that exclusion shows the real backdrop under our own rect (not black):
            // logged once, ~1s in, rather than periodically -- every readback is a GPU->CPU Map
            // that stalls the pipeline, which is visible as hitching at display-rate rendering.
            var proof = DispatcherQueue.GetForCurrentThread().CreateTimer();
            proof.Interval = TimeSpan.FromSeconds(1);
            proof.IsRepeating = false;
            proof.Tick += (_, _) =>
            {
                var itemSize = _capture.ItemSize;
                var ownPxX = (int)(_uvRect.X * itemSize.Width) + _d3d!.Width / 2;
                var ownPxY = (int)(_uvRect.Y * itemSize.Height) + _d3d.Height / 2;
                _capture.LogRegionPixel(ownPxX, ownPxY, "under-window-center");
                _capture.LogRegionPixel(ownPxX + _d3d.Width + 40, ownPxY, "just-outside-window");
            };
            proof.Start();
            return;
        }

        // First snapshot immediately (rather than waiting the full timer interval) so the window
        // doesn't sit black/empty for several seconds after launch; then refresh periodically.
        _ = RefreshSnapshotAsync();
        _snapshotTimer = DispatcherQueue.GetForCurrentThread().CreateTimer();
        _snapshotTimer.Interval = TimeSpan.FromSeconds(4);
        _snapshotTimer.Tick += (_, _) => _ = RefreshSnapshotAsync();
        _snapshotTimer.Start();
    }

    /// <summary>
    /// Hide-then-snapshot, FALLBACK ONLY (see ExcludeFromCaptureMode): built when exclusion was
    /// thought to paint the window's region black in the capture. Briefly hides both
    /// windows (the glass HWND and its WinUI control overlay, which sits at the same screen rect),
    /// waits long enough for DWM to composite a frame without them and for that frame to actually
    /// reach the capture pipeline, freezes it as the new snapshot, then shows both windows again.
    /// Trades true per-frame live animation for a periodic refresh with a brief visible flicker --
    /// the option chosen over dropping self-exclusion outright (real feedback-loop risk) or a
    /// deeper rework of the capture approach.
    /// </summary>
    private async Task RefreshSnapshotAsync()
    {
        if (_window is null || _overlay is null || _capture is null || !_captureReady)
        {
            return;
        }

        // Reentrancy guard: the initial immediate call and the timer's first tick could otherwise
        // overlap if a refresh is still in flight (e.g. capture is slow to deliver a fresh frame).
        if (_snapshotRefreshInFlight)
        {
            return;
        }
        _snapshotRefreshInFlight = true;

        try
        {
            var framesBeforeHide = _capture.FrameCount;
            _window.SetVisible(false);
            _overlay.SetVisible(false);

            // At ~12fps a frame arrives roughly every 83ms; 250ms gives DWM + the capture pipeline
            // a comfortable couple of frames' margin to actually deliver one composited without
            // our windows, rather than racing a single interval.
            await Task.Delay(250);

            var framesWhileHidden = _capture.FrameCount - framesBeforeHide;
            _capture.RefreshFrozenSnapshot();
            File.AppendAllText(_logPath, $"[{DateTimeOffset.Now:O}] [diag] snapshot refreshed: capture frames arrived while hidden={framesWhileHidden}\n");

            // The verification the live-texture diagnostic below can't give: with the window no
            // longer capture-excluded, the live texture under our rect now shows the window itself,
            // so only the frozen copy (taken while hidden) says whether hide-then-snapshot works.
            var itemSize = _capture.ItemSize;
            var ownPxX = (int)(_uvRect.X * itemSize.Width) + _d3d!.Width / 2;
            var ownPxY = (int)(_uvRect.Y * itemSize.Height) + _d3d.Height / 2;
            _capture.LogRegionPixel(ownPxX, ownPxY, "under-window-center", frozen: true);
            _capture.LogRegionPixel(ownPxX + _d3d.Width + 40, ownPxY, "just-outside-window", frozen: true);
        }
        finally
        {
            _window.SetVisible(true);
            _overlay.SetVisible(true);
            _snapshotRefreshInFlight = false;
        }
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
    private int _dumpPollCounter;
    private bool _dimProbeInFlight;

    /// <summary>
    /// Instrument for the "faint global dim" (2026-09-14): the panel interior is meant to be an
    /// identity copy of the backdrop, so at interior points away from the bezel and the card,
    /// composite-on-screen minus clean-backdrop is the total dim, and the same reading with the
    /// overlay hidden attributes it between the glass renderer and the WinUI overlay. The only
    /// way to read the composite is to briefly un-exclude both windows from capture (so the live
    /// frame includes them); the render timer is paused meanwhile so the glass can't feed back
    /// on itself and compound whatever dim there is. ~0.5s, logged, then everything restored.
    /// </summary>
    private async Task MeasureDimAsync()
    {
        if (_window is null || _overlay is null || _capture is null || _d3d is null || !_captureReady) return;
        _dimProbeInFlight = true;
        try
        {
            var itemSize = _capture.ItemSize;
            var originX = (int)(_uvRect.X * itemSize.Width);
            var originY = (int)(_uvRect.Y * itemSize.Height);
            // Right-hand third of the window, clear of the card (which sits at the left) and well
            // inside the 16px bezel.
            var points = new (int X, int Y)[]
            {
                (originX + (int)(_d3d.Width * 0.85), originY + (int)(_d3d.Height * 0.30)),
                (originX + (int)(_d3d.Width * 0.85), originY + (int)(_d3d.Height * 0.50)),
                (originX + (int)(_d3d.Width * 0.85), originY + (int)(_d3d.Height * 0.70)),
                (originX + (int)(_d3d.Width * 0.70), originY + (int)(_d3d.Height * 0.90)),
            };

            _window.StopRenderTimer();
            var clean = points.Select(p => _capture.ReadPixel(p.X, p.Y)).ToArray();

            WindowCaptureExclusion.SetExcluded(_window.Handle, false);
            WindowCaptureExclusion.SetExcluded(_overlay.Handle, false);
            await Task.Delay(200);
            var composite = points.Select(p => _capture.ReadPixel(p.X, p.Y)).ToArray();

            _overlay.SetVisible(false);
            await Task.Delay(200);
            var glassOnly = points.Select(p => _capture.ReadPixel(p.X, p.Y)).ToArray();

            _overlay.SetVisible(true);
            WindowCaptureExclusion.SetExcluded(_window.Handle, true);
            WindowCaptureExclusion.SetExcluded(_overlay.Handle, true);

            var sb = new System.Text.StringBuilder();
            sb.AppendLine($"[{DateTimeOffset.Now:O}] [dimprobe] clean backdrop vs composite (glass+overlay) vs glass alone, (B,G,R):");
            for (var i = 0; i < points.Length; i++)
            {
                static string F((byte B, byte G, byte R, byte A)? p) => p is { } v ? $"({v.B},{v.G},{v.R})" : "n/a";
                static string D((byte B, byte G, byte R, byte A)? a, (byte B, byte G, byte R, byte A)? b) =>
                    a is { } x && b is { } y ? $"({y.B - x.B:+0;-0;0},{y.G - x.G:+0;-0;0},{y.R - x.R:+0;-0;0})" : "n/a";
                sb.AppendLine($"  pt{i} @({points[i].X},{points[i].Y}): clean={F(clean[i])} composite={F(composite[i])} d={D(clean[i], composite[i])}  glassOnly={F(glassOnly[i])} d={D(clean[i], glassOnly[i])}");
            }
            File.AppendAllText(_logPath, sb.ToString());
        }
        catch (Exception ex)
        {
            File.AppendAllText(_logPath, $"[{DateTimeOffset.Now:O}] [dimprobe] threw: {ex}\n");
        }
        finally
        {
            _window?.StartRenderTimer(16);
            _dimProbeInFlight = false;
        }
    }

    private void RenderTick()
    {
        var srv = ExcludeFromCaptureMode ? _capture?.TryGetLiveFrameSrv() : _capture?.TryGetCurrentFrameSrv();
        if (srv is null || _renderer is null || _d3d is null)
        {
            return;
        }

        var controlShapes = GlassShapeRegistry.Snapshot();
        var shapes = new GlassShape[1 + controlShapes.Length];
        shapes[0] = _renderer.PanelShape(_d3d.Width, _d3d.Height);
        controlShapes.CopyTo(shapes, 1);
        _renderer.Draw(_d3d.RenderTargetView, srv, _uvRect, _d3d.Width, _d3d.Height, shapes);

        // Debug hooks, checked every ~half second, not every tick: "dump.txt" next to the exe
        // requests one raw frame dump (see D3D11Context.DumpBackBuffer); "dimprobe.txt" runs the
        // global-dim measurement (see MeasureDimAsync).
        if (++_dumpPollCounter % 30 == 0)
        {
            var trigger = Path.Combine(AppContext.BaseDirectory, "dump.txt");
            if (File.Exists(trigger))
            {
                File.Delete(trigger);
                _d3d.DumpBackBuffer(Path.Combine(AppContext.BaseDirectory, $"frame-{_d3d.Width}x{_d3d.Height}.bgra"));
            }
            var probe = Path.Combine(AppContext.BaseDirectory, "dimprobe.txt");
            if (File.Exists(probe) && !_dimProbeInFlight)
            {
                File.Delete(probe);
                _ = MeasureDimAsync();
            }
        }

        _d3d.Present();

        var frameCount = _capture!.FrameCount;
        if (frameCount != _loggedFrameCount && (frameCount == 1 || frameCount % 12 == 0))
        {
            _loggedFrameCount = frameCount;
            File.AppendAllText(_logPath, $"[{DateTimeOffset.Now:O}] rendered frameCount={frameCount}\n");
        }
    }
}
