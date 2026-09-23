using System.Numerics;
using System.Runtime.InteropServices;
using Microsoft.UI;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using Vortice.DXGI;
using Windows.Win32;
using Windows.Win32.Foundation;
using Windows.Win32.Graphics.Dwm;
using Windows.Win32.UI.Shell;

namespace Jarvis_Glass;

/// <summary>
/// Puts a WinUI window on the liquid-glass stack. The window's XAML tree draws only the
/// controls' chrome and text over a transparent backdrop, while everything glass -- panel slabs,
/// control tracks, thumbs, refracted labels -- is rendered by <see cref="GlassRenderer"/> into a
/// premultiplied-alpha DXGI swapchain that a <see cref="SwapChainPanel"/> at the very bottom of
/// the window's content composites under the rest of the tree. Because the glass lives in the
/// window's own composition tree it moves with the window in the same DWM frame; the earlier
/// design (a separate owner-linked HWND repositioned from <c>AppWindow.Changed</c>) always
/// lagged the chrome by a frame while dragging. Controls publish geometry through the window's
/// <see cref="Scene"/>; the render tick is <see cref="CompositionTarget.Rendering"/>, so it runs
/// on the XAML thread once per display frame.
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
    private readonly SwapChainPanel _panel;
    private readonly D3D11Context _d3d;
    private readonly IDXGISwapChain2 _swapChain2;
    private readonly LiveCaptureSource _capture;
    private readonly GlassRenderer _renderer;
    private Vector4 _uvRect = new(0, 0, 1, 1);
    private bool _captureReady;
    private bool _rendering;
    private (int Capture, int Scene, Vector4 Uv, int W, int H) _lastRendered = (-1, -1, default, 0, 0);
    private readonly CaptureChangeDetector _detector;
    private CropRect _crop;
    private int _insideChecked = -1;
    private int _confirmedInsideChanges;
    private bool _disposed;

    public GlassScene Scene { get; } = new();

    private unsafe nint WindowHandleValue => (nint)_windowHwnd.Value;

    /// <param name="window">A constructed, not-yet-shown WinUI window whose <c>Content</c> is
    /// already set -- it is re-parented under a Grid with the glass panel beneath it.</param>
    public GlassHost(Window window)
    {
        var hwndValue = WinRT.Interop.WindowNative.GetWindowHandle(window);
        _windowHwnd = (HWND)hwndValue;
        _appWindow = AppWindow.GetFromWindowId(Win32Interop.GetWindowIdFromWindow(hwndValue));
        _windowId = _appWindow.Id.Value;
        _subclassProc = SubclassProc;

        GlassLog.Path ??= Path.Combine(AppContext.BaseDirectory, "jarvis-glass.log");

        var client = ClientRectOnScreen();
        var width = Math.Max(1, client.Width);
        var height = Math.Max(1, client.Height);

        _d3d = new D3D11Context(width, height);
        _swapChain2 = _d3d.SwapChain.QueryInterface<IDXGISwapChain2>();
        _capture = new LiveCaptureSource(_d3d);
        _renderer = new GlassRenderer(_d3d.Device, _d3d.ImmediateContext, Path.Combine(AppContext.BaseDirectory, "Shaders"));
        _detector = new CaptureChangeDetector(_d3d.Device, _d3d.ImmediateContext, Path.Combine(AppContext.BaseDirectory, "Shaders"));

        // The glass panel goes under the window's existing content. Hit-testing stays with the
        // XAML above it (and falls through to nothing where there is no XAML, same as before).
        _panel = new SwapChainPanel
        {
            IsHitTestVisible = false,
            HorizontalAlignment = HorizontalAlignment.Stretch,
            VerticalAlignment = VerticalAlignment.Stretch,
        };
        var content = window.Content as UIElement;
        var root = new Grid { Background = null };
        window.Content = root;
        root.Children.Add(_panel);
        if (content is not null) root.Children.Add(content);
        SwapChainPanelInterop.SetSwapChain(_panel, _d3d.SwapChain);
        ApplyCompositionScale();
        _panel.CompositionScaleChanged += (_, _) => ApplyCompositionScale();
        _panel.SizeChanged += (_, _) => SyncToWindow();

        // The WinUI window: transparent where XAML paints nothing.
        window.SystemBackdrop = new TransparentBackdrop();
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
        GlassLog.Write($"host for window 0x{_windowId:X} '{window.Title}' registered scene 0x{Scene.GetHashCode():X}");

        _appWindow.Changed += OnAppWindowChanged;
        InstallSubclass();
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
            var ok = await _capture.StartAsync(_windowHwnd);
            if (!ok || _disposed)
            {
                GlassLog.Write("live capture failed to start (consent denied or API unavailable); glass stays empty");
                return;
            }
            _captureReady = true;
            RecomputeUvRect();
            // Out of every capture pipeline (ours included) so the glass reads the real backdrop
            // under the window rather than its own last frame.
            WindowCaptureExclusion.Register(WindowHandleValue);
            WindowCaptureExclusion.SetExcluded(true);
            CompositionTarget.Rendering += OnRendering;
        }
        catch (Exception ex)
        {
            GlassLog.Write($"StartCaptureAsync threw: {ex}");
        }
    }

    private void OnRendering(object? sender, object e) => RenderTick();

    private void OnAppWindowChanged(AppWindow sender, AppWindowChangedEventArgs args)
    {
        if (args.DidPositionChange || args.DidSizeChange || args.DidVisibilityChange || args.DidPresenterChange)
        {
            SyncToWindow();
        }
    }

    // ---- WM_DISPLAYCHANGE via a comctl32 subclass of the WinUI HWND ------------------------
    // A monitor being added/removed/reconfigured can leave the window on a different display
    // without it moving, and the capture item is per-display. The delegate is kept in a field
    // so its thunk outlives the call.

    private readonly SUBCLASSPROC _subclassProc;
    private const nuint SubclassId = 0x4A47; // 'JG'

    private void InstallSubclass()
    {
        PInvoke.SetWindowSubclass(_windowHwnd, _subclassProc, SubclassId, 0);
    }

    private LRESULT SubclassProc(HWND hwnd, uint msg, WPARAM wParam, LPARAM lParam, nuint id, nuint refData)
    {
        if (msg == PInvoke.WM_DISPLAYCHANGE && !_disposed)
        {
            _capture.RetargetIfDisplayChanged(WindowHandleValue);
            RecomputeUvRect();
        }
        return PInvoke.DefSubclassProc(hwnd, msg, wParam, lParam);
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

    /// <summary>The swapchain is sized in physical pixels but a SwapChainPanel maps buffer pixels
    /// to DIPs 1:1 by default, so the inverse composition scale has to be applied or the glass
    /// renders at (scale)x the window on a HiDPI display.</summary>
    private void ApplyCompositionScale()
    {
        var sx = _panel.CompositionScaleX;
        var sy = _panel.CompositionScaleY;
        if (sx <= 0 || sy <= 0) return;
        _swapChain2.MatrixTransform = Matrix3x2.CreateScale(1f / sx, 1f / sy);
    }

    private void SyncToWindow()
    {
        if (_disposed) return;
        var visible = PInvoke.IsWindowVisible(_windowHwnd) && !PInvoke.IsIconic(_windowHwnd);
        var client = ClientRectOnScreen();
        if (!visible || client.Width <= 0 || client.Height <= 0)
        {
            _visible = false;
            _capture.Paused = true;
            return;
        }
        _capture.Paused = false;

        var resized = client.Width != _d3d.Width || client.Height != _d3d.Height;
        if (resized)
        {
            _d3d.ResizeBuffers(client.Width, client.Height);
        }
        _capture.RetargetIfDisplayChanged(WindowHandleValue);
        RecomputeUvRect();
        var wasVisible = _visible;
        _visible = true;
        if (_captureReady && (resized || !wasVisible))
        {
            // A resize or first show needs a fresh frame now, not a tick later, or the resized
            // buffers show up stale/black. A pure move does NOT render here: AppWindow.Changed
            // fires per mouse-move during a drag, and a vsync-bound Present on every one fills
            // the present queue and blocks the UI thread. The next Rendering tick re-renders
            // the shifted UV crop within a frame, which is the latency the capture already has.
            RenderTick();
        }
    }

    private bool _visible;

    private void RecomputeUvRect()
    {
        if (!_captureReady) return;
        var item = _capture.ItemSize;
        if (item.Width <= 0 || item.Height <= 0) return;

        // The capture is of the display the window is on and its item is that display's pixels,
        // so the client rect is taken relative to that display's origin, not the virtual
        // desktop's.
        var rect = ClientRectOnScreen();
        var originX = 0;
        var originY = 0;
        try
        {
            var area = DisplayArea.GetFromWindowId(_appWindow.Id, DisplayAreaFallback.Nearest);
            originX = area.OuterBounds.X;
            originY = area.OuterBounds.Y;
        }
        catch { }

        _uvRect = new Vector4(
            (float)(rect.X - originX) / item.Width,
            (float)(rect.Y - originY) / item.Height,
            (float)rect.Width / item.Width,
            (float)rect.Height / item.Height);

        // The same rect in capture pixels, clipped to the display: what the capture source
        // classifies dirty rects against and the change detector compares.
        var left = Math.Clamp(rect.X - originX, 0, item.Width);
        var top = Math.Clamp(rect.Y - originY, 0, item.Height);
        var right = Math.Clamp(rect.X - originX + rect.Width, 0, item.Width);
        var bottom = Math.Clamp(rect.Y - originY + rect.Height, 0, item.Height);
        _crop = new CropRect(left, top, right - left, bottom - top);
        _capture.WatchRect = _crop;
    }

    private void RenderTick()
    {
        if (_disposed || _rendering) return;
        // AppWindow.Changed does not reliably report a minimize, and a minimized window kept
        // rendering every capture frame. Two cheap calls per tick settle it either way.
        var shown = PInvoke.IsWindowVisible(_windowHwnd) && !PInvoke.IsIconic(_windowHwnd);
        if (shown != _visible)
        {
            if (shown) SyncToWindow(); // renders the first frame back itself
            else { _visible = false; _capture.Paused = true; }
            return;
        }
        if (!_visible) return;
        var srv = _capture.TryGetLiveFrameSrv();
        if (srv is null) return;

        // Frames whose changes all lie inside the window are usually our own Present echoing
        // back through capture; count them only once the GPU confirms the pixels behind the
        // window differ from what was last rendered (see CaptureChangeDetector).
        if (_detector.TryTakeResult(out var changed) && changed) _confirmedInsideChanges++;
        var inside = _capture.InsideWindowVersion;
        if (inside != _insideChecked && !_detector.Pending)
        {
            _insideChecked = inside;
            if (_detector.Begin(srv, _crop)) _confirmedInsideChanges++;
        }

        // Rendering fires every display frame, but the inputs only change when the desktop under
        // the window changed, a control published, or the window moved or resized. Re-rendering
        // an identical frame is pure GPU heat -- the full pipeline is several passes per layer
        // over every pixel -- so skip it.
        var captureVersion = _capture.ContentVersion + _confirmedInsideChanges;
        var key = (Capture: captureVersion, Scene: Scene.Version, Uv: _uvRect, W: _d3d.Width, H: _d3d.Height);
        if (key == _lastRendered) return;
        // Layer 0 is rebuilt from the capture only when one of these changed (a scene-only change
        // is served from the renderer's layer cache), so only then does the detector's reference
        // move -- otherwise a check still in flight for an unrendered change would be lost.
        var backdropChanged = key.Capture != _lastRendered.Capture || key.Uv != _lastRendered.Uv
            || key.W != _lastRendered.W || key.H != _lastRendered.H;
        _lastRendered = key;
        _rendering = true;
        try
        {
            if (backdropChanged) _detector.Snapshot(_capture, _crop);
            // Layer 0 is a flat, invisible pane over the whole window: no bezel, tint or rim,
            // just an identity copy of the capture. Every layer refracts the layer below it, so
            // without this the slabs on layer 1 would be bending layer 0's transparent nothing
            // and come out black (in the lab the whole-window panel played this role). It also
            // means the gaps between slabs show the live capture rather than DWM's own
            // passthrough -- indistinguishable, bar a frame of latency while the window is
            // being dragged.
            var scene = Scene.SnapshotShapes();
            var shapes = new GlassShape[Math.Min(GlassScene.MaxShapes, scene.Length + 1)];
            shapes[0] = GlassShape.Create(
                new Vector2(_d3d.Width * 0.5f, _d3d.Height * 0.5f), new Vector2(_d3d.Width * 0.5f, _d3d.Height * 0.5f),
                cornerRadius: 0f, bezelWidth: 0f, GlassBezelProfile.Squircle, refractionScale: 0f, specularIntensity: 0f,
                layer: 0, tintColor: Vector3.One, tintAmount: 0f);
            scene.AsSpan(0, shapes.Length - 1).CopyTo(shapes.AsSpan(1));
            _renderer.Draw(_d3d.RenderTargetView, srv, captureVersion, _uvRect, _d3d.Width, _d3d.Height, shapes, Scene.SnapshotTexts());
            _d3d.Present();
        }
        finally
        {
            _rendering = false;
        }
    }

    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;
        CompositionTarget.Rendering -= OnRendering;
        _appWindow.Changed -= OnAppWindowChanged;
        PInvoke.RemoveWindowSubclass(_windowHwnd, _subclassProc, SubclassId);
        GlassScene.Unregister(_windowId);
        _capture.Dispose();
        _detector.Dispose();
        _renderer.Dispose();
        _swapChain2.Dispose();
        _d3d.Dispose();
    }
}

/// <summary>DWM chrome tweaks for hosted windows.</summary>
public static class GlassWindowChrome
{
    private const uint DWMWA_BORDER_COLOR = 34;
    private const uint DWMWA_COLOR_NONE = 0xFFFFFFFE;

    /// <summary>Removes the 1px DWM outline Windows 11 draws around a top-level window. The glass
    /// panel paints the whole client rect (layer 0 is a copy of the desktop behind it), so DWM
    /// treats the window as opaque and outlines it -- on a borderless card like the HUD that
    /// reads as a rectangle around the pill.</summary>
    public static unsafe void HideBorder(Window window)
    {
        var hwnd = (HWND)WinRT.Interop.WindowNative.GetWindowHandle(window);
        var color = DWMWA_COLOR_NONE;
        PInvoke.DwmSetWindowAttribute(hwnd, (DWMWINDOWATTRIBUTE)DWMWA_BORDER_COLOR, &color, sizeof(uint));
    }
}

/// <summary>ISwapChainPanelNative (microsoft.ui.xaml.media.dxinterop.h) by raw vtable: the
/// interface isn't projected, and a bare QueryInterface + slot call avoids depending on how
/// CsWinRT marshals ComImport interfaces.</summary>
internal static unsafe class SwapChainPanelInterop
{
    // {63aad0b8-7c24-40ff-85a8-640d944cc325}
    private static readonly Guid IID_ISwapChainPanelNative = new(0x63aad0b8, 0x7c24, 0x40ff, 0x85, 0xa8, 0x64, 0x0d, 0x94, 0x4c, 0xc3, 0x25);

    public static void SetSwapChain(SwapChainPanel panel, IDXGISwapChain1 swapChain)
    {
        var unknown = WinRT.MarshalInspectable<object>.FromManaged(panel);
        try
        {
            Marshal.ThrowExceptionForHR(Marshal.QueryInterface(unknown, in IID_ISwapChainPanelNative, out var native));
            try
            {
                // IUnknown: QueryInterface/AddRef/Release = slots 0..2; SetSwapChain = slot 3.
                var vtbl = *(void***)native;
                var setSwapChain = (delegate* unmanaged[Stdcall]<nint, nint, int>)vtbl[3];
                Marshal.ThrowExceptionForHR(setSwapChain(native, swapChain.NativePointer));
            }
            finally
            {
                Marshal.Release(native);
            }
        }
        finally
        {
            Marshal.Release(unknown);
        }
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
