using System.Runtime.InteropServices;
using Microsoft.UI;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Media;
using Windows.Graphics;
using Windows.Win32;
using Windows.Win32.Foundation;
using Windows.Win32.UI.WindowsAndMessaging;

namespace Jarvis_GlassLab;

/// <summary>
/// Phase 7: a normal WinUI3 window hosting real controls, stacked above the glass HWND rather
/// than merged into its composition tree (see the GlassLab plan doc for why - merging a raw
/// DirectComposition visual into WinUI3's own XAML compositor is undocumented/fragile territory,
/// while two owner-linked, DWM-stacked windows is a well-understood pattern). Transparent
/// background (not WS_EX_LAYERED - CLAUDE.md's own known-gotchas note that WS_EX_LAYERED
/// color-keying on a WinUI3 XAML island is unsupported; a borderless window with a transparent
/// XAML background composites correctly on its own since modern WinUI3 windows already render via
/// their own DirectComposition-backed surface). Click-through everywhere except the test button's
/// actual screen bounds, via a subclassed WndProc intercepting WM_NCHITTEST.
/// </summary>
public sealed partial class ControlOverlayWindow : Window
{
    private WNDPROC? _subclassProc;
    private WNDPROC _originalWndProc = null!;
    private HWND _hwnd;
    private AppWindow _appWindow = null!;

    public ControlOverlayWindow(nint ownerHwndValue)
    {
        InitializeComponent();

        var hwndValue = WinRT.Interop.WindowNative.GetWindowHandle(this);
        _hwnd = (HWND)hwndValue;

        var windowId = Win32Interop.GetWindowIdFromWindow(hwndValue);
        _appWindow = AppWindow.GetFromWindowId(windowId);

        // Borderless, no title bar, no resize/maximize/minimize chrome, always on top, hidden
        // from Alt+Tab/taskbar - this window exists purely to host controls above the glass
        // layer, not to be a normal app window in its own right.
        if (_appWindow.Presenter is OverlappedPresenter presenter)
        {
            presenter.SetBorderAndTitleBar(false, false);
            presenter.IsResizable = false;
            presenter.IsMaximizable = false;
            presenter.IsMinimizable = false;
            presenter.IsAlwaysOnTop = true;
        }
        _appWindow.IsShownInSwitchers = false;

        // Owns this window to the glass HWND so they minimize/close together and stack correctly,
        // without making this a child window (which would clip it to the parent's bounds).
        PInvoke.SetWindowLongPtr(_hwnd, WINDOW_LONG_PTR_INDEX.GWLP_HWNDPARENT, ownerHwndValue);

        SubclassWndProc();
    }

    /// <summary>Keeps this window pixel-locked to the glass window's screen rect. Called from
    /// App whenever the glass window's position/size changes.</summary>
    public void SyncBounds(int x, int y, int width, int height)
    {
        _appWindow.MoveAndResize(new RectInt32(x, y, width, height));
    }

    private void TestButton_Click(object sender, RoutedEventArgs e)
    {
        TestButton.Content = TestButton.Content is "Clicked!" ? "GlassLab test button" : "Clicked!";
        var logPath = LiveCaptureSource.DiagnosticLogPath;
        if (logPath is not null)
        {
            try { File.AppendAllText(logPath, $"[{DateTimeOffset.Now:O}] ControlOverlayWindow: test button clicked\n"); } catch { }
        }
    }

    private void SubclassWndProc()
    {
        _subclassProc = WndProc;
        var newProcPtr = Marshal.GetFunctionPointerForDelegate(_subclassProc);
        var originalPtr = PInvoke.SetWindowLongPtr(_hwnd, WINDOW_LONG_PTR_INDEX.GWLP_WNDPROC, newProcPtr);
        _originalWndProc = Marshal.GetDelegateForFunctionPointer<WNDPROC>(originalPtr);
    }

    private LRESULT WndProc(HWND hwnd, uint msg, WPARAM wParam, LPARAM lParam)
    {
        if (msg == PInvoke.WM_NCHITTEST)
        {
            var screenX = unchecked((short)(lParam.Value & 0xFFFF));
            var screenY = unchecked((short)((lParam.Value >> 16) & 0xFFFF));
            return new LRESULT(IsPointOverControl(screenX, screenY) ? HTCLIENT : HTTRANSPARENT);
        }

        return PInvoke.CallWindowProc(_originalWndProc, hwnd, msg, wParam, lParam);
    }

    /// <summary>
    /// Translates the button's XAML-space bounds to screen pixels and tests against them. Only
    /// one control exists in Phase 7 - a real implementation with multiple interactive regions
    /// would maintain a list here instead, per the plan's "maintained hit-region list" note.
    /// </summary>
    private bool IsPointOverControl(int screenX, int screenY)
    {
        try
        {
            var scale = TestButton.XamlRoot?.RasterizationScale ?? 1.0;
            var transform = TestButton.TransformToVisual(RootGrid);
            var bounds = transform.TransformBounds(new Windows.Foundation.Rect(0, 0, TestButton.ActualWidth, TestButton.ActualHeight));

            var pos = _appWindow.Position;
            var left = pos.X + (int)(bounds.X * scale);
            var top = pos.Y + (int)(bounds.Y * scale);
            var right = left + (int)(bounds.Width * scale);
            var bottom = top + (int)(bounds.Height * scale);

            return screenX >= left && screenX < right && screenY >= top && screenY < bottom;
        }
        catch
        {
            // XamlRoot not ready yet (e.g. very first hit-test before layout has run) - default
            // to click-through rather than accidentally swallowing a click meant for the desktop.
            return false;
        }
    }

    private const int HTCLIENT = 1;
    private const int HTTRANSPARENT = -1;
}
