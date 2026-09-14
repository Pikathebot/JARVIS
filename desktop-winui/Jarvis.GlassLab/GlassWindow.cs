using System.Runtime.InteropServices;
using Windows.Win32;
using Windows.Win32.Foundation;
using Windows.Win32.Graphics.Dwm;
using Windows.Win32.Graphics.Gdi;
using Windows.Win32.UI.WindowsAndMessaging;

namespace Jarvis_GlassLab;

/// <summary>
/// Phase 1: a bare, fully transparent, click-through top-level window with no D3D11 rendering
/// yet. A plain Win32 HWND (not a Microsoft.UI.Xaml.Window) so the window never gets a WinUI3
/// XAML redirection surface — see the GlassLab plan doc for why. Window creation order here is
/// load-bearing: DWM must be told this HWND participates in blur-behind/layered composition
/// before anything else touches it, or the first frame can show a flash of the default opaque
/// background.
/// </summary>
internal sealed unsafe class GlassWindow
{
    private const string ClassName = "JarvisGlassLabWindowClass";

    // Kept alive for the lifetime of the window so the delegate->function-pointer thunk GC can't
    // collect isn't reclaimed out from under DispatchMessage.
    private readonly WNDPROC _wndProc;
    private HWND _hwnd;

    public HWND Handle => _hwnd;

    public nint HandleValue => (nint)_hwnd.Value;

    /// <summary>Invoked on WM_TIMER, i.e. on the same thread as RunMessageLoop. Set before
    /// starting the render timer.</summary>
    public Action? OnRenderTick { get; set; }

    private const nuint RenderTimerId = 1;

    public void StartRenderTimer(uint intervalMs)
    {
        PInvoke.SetTimer(_hwnd, RenderTimerId, intervalMs, null);
    }

    public void StopRenderTimer()
    {
        PInvoke.KillTimer(_hwnd, RenderTimerId);
    }

    public RECT GetScreenRect()
    {
        PInvoke.GetWindowRect(_hwnd, out var rect);
        return rect;
    }

    /// <summary>Invoked after the window's size and/or position actually changed (WM_WINDOWPOSCHANGED
    /// fires post-facto, so GetScreenRect() inside the handler already reflects the new state).
    /// Fires redundantly on pure moves too - callers should diff against their own cached
    /// size/position rather than assume every call means a resize.</summary>
    public Action? OnWindowPosChanged { get; set; }

    /// <summary>Invoked on WM_DISPLAYCHANGE (a monitor was added/removed/reconfigured) - a coarser
    /// signal than WM_WINDOWPOSCHANGED, used to re-check which display the window is actually on
    /// even when the window itself didn't move.</summary>
    public Action? OnDisplayChanged { get; set; }

    /// <summary>
    /// Programmatic resize - this window has no visible chrome for a user to drag-resize by (it's
    /// borderless and click-through), so this is the debug/test affordance the plan calls for
    /// until Phase 7 adds real interactive chrome.
    /// </summary>
    public void SetSize(int width, int height)
    {
        PInvoke.SetWindowPos(_hwnd, HWND.Null, 0, 0, width, height,
            SET_WINDOW_POS_FLAGS.SWP_NOMOVE | SET_WINDOW_POS_FLAGS.SWP_NOZORDER | SET_WINDOW_POS_FLAGS.SWP_NOACTIVATE);
    }

    /// <summary>Briefly hidden around each periodic snapshot capture (see App.RefreshSnapshotAsync)
    /// instead of the old always-on WDA_EXCLUDEFROMCAPTURE -- excluding a window from capture
    /// paints that window's own screen region solid black in the capture, which is exactly the
    /// region this window needs to read to refract what's behind it. Actually hiding it removes it
    /// from DWM's composited scene entirely for that one instant, so the capture sees the real
    /// desktop there instead.</summary>
    public void SetVisible(bool visible)
    {
        PInvoke.ShowWindow(_hwnd, visible ? SHOW_WINDOW_CMD.SW_SHOWNOACTIVATE : SHOW_WINDOW_CMD.SW_HIDE);
    }

    public GlassWindow()
    {
        _wndProc = WndProc;
    }

    public void Create()
    {
        var hInstance = PInvoke.GetModuleHandle((string?)null);

        fixed (char* className = ClassName)
        {
            var wc = new WNDCLASSEXW
            {
                cbSize = (uint)Marshal.SizeOf<WNDCLASSEXW>(),
                lpfnWndProc = _wndProc,
                hInstance = (HINSTANCE)hInstance.DangerousGetHandle(),
                lpszClassName = className,
                hCursor = PInvoke.LoadCursor((HINSTANCE)null, PInvoke.IDC_ARROW),
            };

            ushort atom = PInvoke.RegisterClassEx(in wc);
            if (atom == 0)
            {
                throw new InvalidOperationException($"RegisterClassEx failed: {Marshal.GetLastWin32Error()}");
            }
        }

        // WS_EX_LAYERED: required bookkeeping for a layered/composited window (the real alpha
        // comes from the DirectComposition swapchain content added in Phase 2, not GDI blending).
        // WS_EX_NOREDIRECTIONBITMAP: no DWM redirection bitmap - content is supplied purely via
        // DirectComposition/DXGI, which is what makes true per-pixel desktop compositing possible.
        // WS_EX_TRANSPARENT: click-through by default - every point on this window is invisible
        // to hit-testing, so clicks fall through to whatever is beneath it in Z-order.
        // WS_EX_TOOLWINDOW: keeps it out of the taskbar and Alt+Tab (this is a background glass
        // layer, not a normal app window).
        var exStyle = WINDOW_EX_STYLE.WS_EX_LAYERED
            | WINDOW_EX_STYLE.WS_EX_NOREDIRECTIONBITMAP
            | WINDOW_EX_STYLE.WS_EX_TRANSPARENT
            | WINDOW_EX_STYLE.WS_EX_TOOLWINDOW
            | WINDOW_EX_STYLE.WS_EX_TOPMOST;

        _hwnd = PInvoke.CreateWindowEx(
            exStyle,
            ClassName,
            "Jarvis GlassLab",
            WINDOW_STYLE.WS_POPUP,
            100, 100, 960, 760,
            HWND.Null,
            null,
            hInstance,
            null);

        if (_hwnd.IsNull)
        {
            throw new InvalidOperationException($"CreateWindowEx failed: {Marshal.GetLastWin32Error()}");
        }

        // Layered windows must carry SetLayeredWindowAttributes bookkeeping even when the actual
        // per-pixel alpha is supplied by DirectComposition/the swapchain rather than this legacy
        // GDI alpha-blend path - 255/LWA_ALPHA here is a no-op tint, not the real transparency.
        PInvoke.SetLayeredWindowAttributes(_hwnd, default, 255, LAYERED_WINDOW_ATTRIBUTES_FLAGS.LWA_ALPHA);

        var blurBehind = new DWM_BLURBEHIND
        {
            dwFlags = PInvoke.DWM_BB_ENABLE,
            fEnable = true,
            hRgnBlur = HRGN.Null,
        };
        PInvoke.DwmEnableBlurBehindWindow(_hwnd, in blurBehind);

        // SW_SHOWNOACTIVATE: this window should never take focus/steal keyboard input from
        // whatever the user is actually working in.
        PInvoke.ShowWindow(_hwnd, SHOW_WINDOW_CMD.SW_SHOWNOACTIVATE);
    }

    /// <summary>
    /// Blocking Win32 message loop. This app has no Microsoft.UI.Xaml.Window/XAML content, so
    /// there is no competing WinAppSDK-owned pump to coexist with in Phase 1 - this loop is the
    /// only thing driving the thread's message queue. Returns when WM_QUIT is posted (i.e. once
    /// the window is destroyed).
    /// </summary>
    public void RunMessageLoop()
    {
        MSG msg;
        while (PInvoke.GetMessage(&msg, HWND.Null, 0, 0))
        {
            PInvoke.TranslateMessage(&msg);
            PInvoke.DispatchMessage(&msg);
        }
    }

    private LRESULT WndProc(HWND hwnd, uint msg, WPARAM wParam, LPARAM lParam)
    {
        switch (msg)
        {
            case PInvoke.WM_NCHITTEST:
                // Always click-through in Phase 1 - there is nothing interactive to hit yet.
                // Phase 7 replaces this with a maintained hit-region list once real controls exist.
                return new LRESULT((nint)HTTRANSPARENT);

            case PInvoke.WM_DESTROY:
                PInvoke.PostQuitMessage(0);
                return new LRESULT(0);

            case PInvoke.WM_TIMER:
                OnRenderTick?.Invoke();
                return new LRESULT(0);

            case PInvoke.WM_WINDOWPOSCHANGED:
                OnWindowPosChanged?.Invoke();
                break; // still needs DefWindowProc for WM_SIZE/WM_MOVE bookkeeping it triggers internally

            case PInvoke.WM_DISPLAYCHANGE:
                OnDisplayChanged?.Invoke();
                break;
        }

        return PInvoke.DefWindowProc(hwnd, msg, wParam, lParam);
    }

    private const int HTTRANSPARENT = -1;
}
