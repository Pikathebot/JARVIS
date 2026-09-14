using System.Numerics;
using System.Runtime.InteropServices;
using Microsoft.UI;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Windows.Win32;
using Windows.Win32.Foundation;
using Windows.Win32.Graphics.Dwm;
using Windows.Win32.UI.WindowsAndMessaging;

namespace Jarvis_Glass;

/// <summary>
/// Puts a WinUI window on the liquid-glass stack. The window itself becomes the "overlay role"
/// from the GlassLab: its XAML tree draws only the controls' chrome and text over a transparent
/// backdrop, while everything glass -- panel slabs, control tracks, thumbs, refracted labels --
/// is rendered by <see cref="GlassRenderer"/> into a <see cref="GlassBackdropWindow"/> that sits
/// directly behind it. The WinUI window is owner-linked to that HWND so the pair stack and
/// z-order together (an owned window is always above its owner), and this host keeps the glass
/// HWND pixel-locked to the WinUI window's client rect and hidden with it. Controls publish
/// geometry through the window's <see cref="Scene"/>; the render tick runs on the glass HWND's
/// WM_TIMER, pumped by the WinUI thread's own message loop.
///
/// The two DWM details that took the lab days to find are both here: the WinUI window needs a
/// zero-alpha system-compositor backdrop brush (<see cref="TransparentBackdrop"/>) AND
/// DwmEnableBlurBehindWindow with an EMPTY region -- a null region tints the whole window ~x0.75.
/// </summary>
public sealed class GlassHost : IDisposable
{
    private readonly AppWindow _appWindow;
    private readonly HWND _windowHwnd;
    private readonly ulong _windowId;
    private readonly GlassBackdropWindow _glass;
    private readonly D3D11Context _d3d;
    private readonly CompositionContext _composition;
    private readonly LiveCaptureSource _capture;
    private readonly GlassRenderer _renderer;
    private Vector4 _uvRect = new(0, 0, 1, 1);
    private bool _captureReady;
    private bool _disposed;

    public GlassScene Scene { get; } = new();

    private unsafe nint WindowHandleValue => (nint)_windowHwnd.Value;

    /// <summary>Render-rate interval. WM_TIMER's floor is ~15.6ms, i.e. display rate.</summary>
    private const uint RenderIntervalMs = 16;

    /// <param name="window">A constructed, not-yet-shown WinUI window.</param>
    /// <param name="topmost">Whether the window is always-on-top (the HUD). The glass HWND must
    /// share the z-band or the owned WinUI window would drag it up anyway.</param>
    /// <param name="showInTaskbar">Owned windows are dropped from the taskbar unless
    /// WS_EX_APPWINDOW says otherwise -- true for the main window.</param>
    public GlassHost(Window window, bool topmost, bool showInTaskbar)
    {
        var hwndValue = WinRT.Interop.WindowNative.GetWindowHandle(window);
        _windowHwnd = (HWND)hwndValue;
        _appWindow = AppWindow.GetFromWindowId(Win32Interop.GetWindowIdFromWindow(hwndValue));
        _windowId = _appWindow.Id.Value;

        GlassLog.Path ??= Path.Combine(AppContext.BaseDirectory, "jarvis-glass.log");

        var client = ClientRectOnScreen();
        var width = Math.Max(1, client.Width);
        var height = Math.Max(1, client.Height);
        _glass = new GlassBackdropWindow();
        _glass.Create(topmost, width, height);
        _glass.SetBounds(client.X, client.Y, width, height);

        _d3d = new D3D11Context(width, height);
        _composition = new CompositionContext(_d3d.DxgiDevice, _glass.HandleValue, _d3d.SwapChain);
        _capture = new LiveCaptureSource(_d3d);
        _renderer = new GlassRenderer(_d3d.Device, _d3d.ImmediateContext, Path.Combine(AppContext.BaseDirectory, "Shaders"));

        // The WinUI window: transparent where XAML paints nothing, owned by the glass HWND.
        window.SystemBackdrop = new TransparentBackdrop();
        PInvoke.SetWindowLongPtr(_windowHwnd, WINDOW_LONG_PTR_INDEX.GWLP_HWNDPARENT, _glass.HandleValue);
        if (showInTaskbar)
        {
            var ex = PInvoke.GetWindowLongPtr(_windowHwnd, WINDOW_LONG_PTR_INDEX.GWL_EXSTYLE);
            PInvoke.SetWindowLongPtr(_windowHwnd, WINDOW_LONG_PTR_INDEX.GWL_EXSTYLE, ex | (nint)WINDOW_EX_STYLE.WS_EX_APPWINDOW);
        }
        var emptyRegion = PInvoke.CreateRectRgn(0, 0, -1, -1);
        var blurBehind = new DWM_BLURBEHIND
        {
            dwFlags = PInvoke.DWM_BB_ENABLE | PInvoke.DWM_BB_BLURREGION,
            fEnable = true,
            hRgnBlur = emptyRegion,
        };
        PInvoke.DwmEnableBlurBehindWindow(_windowHwnd, in blurBehind);
        PInvoke.DeleteObject(emptyRegion);

        GlassScene.Register(_windowId, Scene);

        _appWindow.Changed += OnAppWindowChanged;
        _glass.OnRenderTick = RenderTick;
        _glass.OnDisplayChanged = () => { _capture.RetargetIfDisplayChanged(_glass.HandleValue); RecomputeUvRect(); };
        window.Closed += (_, _) => Dispose();

        SyncToWindow();

        // Capture access is requested only once the window has actually been activated: the
        // consent prompt has to be shown over a visible window of ours, and a request made from
        // a constructor (before Activate(), or for a HUD that starts hidden) comes back
        // DeniedByUser without any prompt appearing.
        window.Activated += OnFirstActivated;
    }

    private bool _captureRequested;

    private void OnFirstActivated(object sender, WindowActivatedEventArgs args)
    {
        if (_captureRequested || args.WindowActivationState == WindowActivationState.Deactivated) return;
        _captureRequested = true;
        ((Window)sender).Activated -= OnFirstActivated;
        _ = StartCaptureAsync();
    }

    private async Task StartCaptureAsync()
    {
        try
        {
            var ok = await _capture.StartAsync(_glass.Handle);
            if (!ok || _disposed)
            {
                GlassLog.Write("live capture failed to start (consent denied or API unavailable); glass stays empty");
                return;
            }
            _captureReady = true;
            RecomputeUvRect();
            // Both HWNDs out of every capture pipeline (ours included) so the glass reads the
            // real backdrop under itself rather than its own last frame.
            WindowCaptureExclusion.Register(_glass.HandleValue);
            WindowCaptureExclusion.Register(WindowHandleValue);
            WindowCaptureExclusion.SetExcluded(true);
            _glass.StartRenderTimer(RenderIntervalMs);
        }
        catch (Exception ex)
        {
            GlassLog.Write($"StartCaptureAsync threw: {ex}");
        }
    }

    private void OnAppWindowChanged(AppWindow sender, AppWindowChangedEventArgs args)
    {
        if (args.DidPositionChange || args.DidSizeChange || args.DidVisibilityChange || args.DidPresenterChange)
        {
            SyncToWindow();
        }
    }

    /// <summary>Client rect of the WinUI window in screen pixels -- the glass covers exactly what
    /// XAML draws into, not the frame.</summary>
    private (int X, int Y, int Width, int Height) ClientRectOnScreen()
    {
        PInvoke.GetClientRect(_windowHwnd, out var rc);
        var origin = new System.Drawing.Point(0, 0);
        PInvoke.ClientToScreen(_windowHwnd, ref origin);
        return (origin.X, origin.Y, rc.right - rc.left, rc.bottom - rc.top);
    }

    private void SyncToWindow()
    {
        if (_disposed) return;
        var visible = PInvoke.IsWindowVisible(_windowHwnd) && !PInvoke.IsIconic(_windowHwnd);
        if (!visible)
        {
            _glass.SetVisible(false);
            _glassVisible = false;
            return;
        }

        var client = ClientRectOnScreen();
        if (client.Width <= 0 || client.Height <= 0)
        {
            _glass.SetVisible(false);
            _glassVisible = false;
            return;
        }

        _glass.SetBounds(client.X, client.Y, client.Width, client.Height);
        var resized = client.Width != _d3d.Width || client.Height != _d3d.Height;
        if (resized)
        {
            _composition.AroundResize(() => _d3d.ResizeBuffers(client.Width, client.Height));
        }
        _capture.RetargetIfDisplayChanged(_glass.HandleValue);
        RecomputeUvRect();
        var wasVisible = _glassVisible;
        _glass.SetVisible(true);
        _glassVisible = true;
        if (_captureReady && (resized || !wasVisible))
        {
            // A resize or first show needs a fresh frame now, not a tick later, or the resized
            // buffers show up stale/black. A pure move does NOT render here: AppWindow.Changed
            // fires per mouse-move during a drag, and a vsync-bound Present on every one fills
            // the present queue and blocks the UI thread -- the WinUI window itself then
            // stutters along behind the glass. The 16ms timer re-renders the shifted UV crop
            // within a frame, which is the latency the capture already has while dragging.
            RenderTick();
        }
    }

    private bool _glassVisible;

    private void RecomputeUvRect()
    {
        if (!_captureReady) return;
        var item = _capture.ItemSize;
        if (item.Width <= 0 || item.Height <= 0) return;

        // The capture is of the display the glass is on and its item is that display's pixels,
        // so the window rect is taken relative to that display's origin, not the virtual
        // desktop's.
        var rect = _glass.GetScreenRect();
        var originX = 0;
        var originY = 0;
        try
        {
            var area = DisplayArea.GetFromWindowId(Win32Interop.GetWindowIdFromWindow(_glass.HandleValue), DisplayAreaFallback.Nearest);
            originX = area.OuterBounds.X;
            originY = area.OuterBounds.Y;
        }
        catch { }

        _uvRect = new Vector4(
            (float)(rect.left - originX) / item.Width,
            (float)(rect.top - originY) / item.Height,
            (float)(rect.right - rect.left) / item.Width,
            (float)(rect.bottom - rect.top) / item.Height);
    }

    private void RenderTick()
    {
        if (_disposed) return;
        var srv = _capture.TryGetLiveFrameSrv();
        if (srv is null) return;

        // Layer 0 is a flat, invisible pane over the whole window: no bezel, tint or rim, just an
        // identity copy of the capture. Every layer refracts the layer below it, so without this
        // the slabs on layer 1 would be bending layer 0's transparent nothing and come out black
        // (in the lab the whole-window panel played this role). It also means the gaps between
        // slabs show the live capture rather than DWM's own passthrough -- indistinguishable,
        // bar a frame of latency while the window is being dragged.
        var scene = Scene.SnapshotShapes();
        var shapes = new GlassShape[Math.Min(GlassScene.MaxShapes, scene.Length + 1)];
        shapes[0] = GlassShape.Create(
            new Vector2(_d3d.Width * 0.5f, _d3d.Height * 0.5f), new Vector2(_d3d.Width * 0.5f, _d3d.Height * 0.5f),
            cornerRadius: 0f, bezelWidth: 0f, GlassBezelProfile.Squircle, refractionScale: 0f, specularIntensity: 0f,
            layer: 0, tintColor: Vector3.One, tintAmount: 0f);
        scene.AsSpan(0, shapes.Length - 1).CopyTo(shapes.AsSpan(1));
        _renderer.Draw(_d3d.RenderTargetView, srv, _uvRect, _d3d.Width, _d3d.Height, shapes, Scene.SnapshotTexts());
        _d3d.Present();
    }

    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;
        _appWindow.Changed -= OnAppWindowChanged;
        GlassScene.Unregister(_windowId);
        _glass.StopRenderTimer();
        _capture.Dispose();
        _renderer.Dispose();
        _composition.Dispose();
        _d3d.Dispose();
        _glass.Destroy();
    }
}

/// <summary>The one way a WinUI3 top-level window's unpainted area actually becomes transparent
/// to DWM: hand the XAML compositor a backdrop brush with zero alpha. Same mechanism as WinUIEx's
/// TransparentTintBackdrop, inlined to avoid the dependency.</summary>
internal sealed partial class TransparentBackdrop : Microsoft.UI.Xaml.Media.SystemBackdrop
{
    private Windows.UI.Composition.Compositor? _systemCompositor;
    private static object? _dispatcherQueueController;

    /// <summary>A system Compositor needs a Windows.System.DispatcherQueue on its thread; the
    /// WinUI thread only has the Microsoft.UI.Dispatching one, so create the system one here
    /// (DQTYPE_THREAD_CURRENT, DQTAT_COM_NONE).</summary>
    private static void EnsureSystemDispatcherQueue()
    {
        if (_dispatcherQueueController is not null || Windows.System.DispatcherQueue.GetForCurrentThread() is not null)
        {
            return;
        }

        var options = new DispatcherQueueOptions
        {
            dwSize = Marshal.SizeOf<DispatcherQueueOptions>(),
            threadType = 2,   // DQTYPE_THREAD_CURRENT
            apartmentType = 0 // DQTAT_COM_NONE
        };
        var hr = CreateDispatcherQueueController(options, out var controller);
        Marshal.ThrowExceptionForHR(hr);
        _dispatcherQueueController = controller;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct DispatcherQueueOptions
    {
        public int dwSize;
        public int threadType;
        public int apartmentType;
    }

    [DllImport("CoreMessaging.dll")]
    private static extern int CreateDispatcherQueueController(DispatcherQueueOptions options, [MarshalAs(UnmanagedType.IUnknown)] out object dispatcherQueueController);

    protected override void OnTargetConnected(Microsoft.UI.Composition.ICompositionSupportsSystemBackdrop connectedTarget, XamlRoot xamlRoot)
    {
        base.OnTargetConnected(connectedTarget, xamlRoot);
        // WinAppSDK 2.4 types the target's SystemBackdrop slot as a *system* (Windows.UI.Composition)
        // brush, so it has to come from a system Compositor on this XAML thread.
        if (_systemCompositor is null)
        {
            EnsureSystemDispatcherQueue();
            _systemCompositor = new Windows.UI.Composition.Compositor();
        }
        connectedTarget.SystemBackdrop = _systemCompositor.CreateColorBrush(Windows.UI.Color.FromArgb(0, 0, 0, 0));
    }

    protected override void OnTargetDisconnected(Microsoft.UI.Composition.ICompositionSupportsSystemBackdrop disconnectedTarget)
    {
        disconnectedTarget.SystemBackdrop = null;
        base.OnTargetDisconnected(disconnectedTarget);
    }
}
