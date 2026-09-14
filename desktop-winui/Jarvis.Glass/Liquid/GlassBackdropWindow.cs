using System.Runtime.InteropServices;
using Windows.Win32;
using Windows.Win32.Foundation;
using Windows.Win32.Graphics.Dwm;
using Windows.Win32.Graphics.Gdi;
using Windows.Win32.UI.WindowsAndMessaging;

namespace Jarvis_Glass;

/// <summary>
/// The bare, fully transparent, click-through top-level HWND the liquid-glass swapchain is
/// composed into -- one per hosted WinUI window, sitting directly BEHIND it (the WinUI window is
/// owner-linked to this one, and owned windows always stack above their owner). A plain Win32
/// HWND rather than a Microsoft.UI.Xaml.Window so it never gets a XAML redirection surface;
/// ported from the GlassLab. Creation order is load-bearing: DWM must be told this HWND
/// participates in blur-behind/layered composition before anything else touches it, or the
/// first frame can flash the default opaque background.
/// </summary>
internal sealed unsafe class GlassBackdropWindow
{
    private const string ClassName = "JarvisGlassBackdropWindowClass";

    // One window class per process, so one static WndProc that routes by HWND -- each host's
    // window is a separate instance. The delegate is static so its thunk lives for the process.
    private static readonly WNDPROC StaticWndProc = WndProc;
    private static readonly Dictionary<nint, GlassBackdropWindow> Instances = new();
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

    /// <summary>Follows the hosted WinUI window: hidden while it is hidden or minimized, since an
    /// owner is not minimized along with the windows it owns.</summary>
    public void SetVisible(bool visible)
    {
        PInvoke.ShowWindow(_hwnd, visible ? SHOW_WINDOW_CMD.SW_SHOWNOACTIVATE : SHOW_WINDOW_CMD.SW_HIDE);
    }

    public GlassBackdropWindow()
    {
    }

    /// <param name="topmost">WS_EX_TOPMOST -- for a window that is itself always-on-top (the
    /// HUD). The owned WinUI window inherits the effective z-band, so this must match it.</param>
    public void Create(bool topmost, int width, int height)
    {
        var hInstance = PInvoke.GetModuleHandle((string?)null);

        fixed (char* className = ClassName)
        {
            var wc = new WNDCLASSEXW
            {
                cbSize = (uint)Marshal.SizeOf<WNDCLASSEXW>(),
                lpfnWndProc = StaticWndProc,
                hInstance = (HINSTANCE)hInstance.DangerousGetHandle(),
                lpszClassName = className,
                hCursor = PInvoke.LoadCursor((HINSTANCE)null, PInvoke.IDC_ARROW),
            };

            // One class per process: the second host (main window + HUD) would otherwise fail
            // with ERROR_CLASS_ALREADY_EXISTS (1410).
            ushort atom = PInvoke.RegisterClassEx(in wc);
            if (atom == 0 && Marshal.GetLastWin32Error() != 1410)
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
            | (topmost ? WINDOW_EX_STYLE.WS_EX_TOPMOST : 0);

        _hwnd = PInvoke.CreateWindowEx(
            exStyle,
            ClassName,
            "Jarvis Glass",
            WINDOW_STYLE.WS_POPUP,
            0, 0, width, height,
            HWND.Null,
            null,
            hInstance,
            null);

        if (_hwnd.IsNull)
        {
            throw new InvalidOperationException($"CreateWindowEx failed: {Marshal.GetLastWin32Error()}");
        }
        // Creation-time messages went to DefWindowProc; everything from here routes to this instance.
        lock (Instances) Instances[HandleValue] = this;

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

        // Shown by GlassHost once it is positioned under its WinUI window (SW_SHOWNOACTIVATE
        // there: this window must never take focus from whatever the user is working in).
    }

    /// <summary>Moves and sizes without activating or changing z-order.</summary>
    public void SetBounds(int x, int y, int width, int height)
    {
        PInvoke.SetWindowPos(_hwnd, HWND.Null, x, y, width, height,
            SET_WINDOW_POS_FLAGS.SWP_NOZORDER | SET_WINDOW_POS_FLAGS.SWP_NOACTIVATE);
    }

    public void Destroy()
    {
        if (!_hwnd.IsNull)
        {
            lock (Instances) Instances.Remove(HandleValue);
            PInvoke.DestroyWindow(_hwnd);
            _hwnd = HWND.Null;
        }
    }

    private static LRESULT WndProc(HWND hwnd, uint msg, WPARAM wParam, LPARAM lParam)
    {
        GlassBackdropWindow? self;
        lock (Instances) Instances.TryGetValue((nint)hwnd.Value, out self);
        if (self is null) return PInvoke.DefWindowProc(hwnd, msg, wParam, lParam);
        return self.HandleMessage(hwnd, msg, wParam, lParam);
    }

    private LRESULT HandleMessage(HWND hwnd, uint msg, WPARAM wParam, LPARAM lParam)
    {
        switch (msg)
        {
            case PInvoke.WM_NCHITTEST:
                // Always click-through in Phase 1 - there is nothing interactive to hit yet.
                // Phase 7 replaces this with a maintained hit-region list once real controls exist.
                return new LRESULT((nint)HTTRANSPARENT);

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
