using System;
using System.Runtime.InteropServices;
using Microsoft.UI.Windowing;

namespace Jarvis_App;

/// <summary>
/// AppWindow sizes (Resize, PreferredMinimumWidth/Height) are in physical pixels, but the layouts
/// inside are in DIPs: a 760 px minimum is 760 DIPs at 100% and only 507 at 150%, where Settings'
/// rows no longer fit. Windows give their sizes in DIPs here and get them scaled for the monitor
/// they are on, and again whenever the window moves to a monitor with another scale.
/// </summary>
internal static class WindowSizing
{
    public static double Scale(IntPtr hwnd)
    {
        var dpi = GetDpiForWindow(hwnd);
        return dpi == 0 ? 1.0 : dpi / 96.0;
    }

    public static Windows.Graphics.SizeInt32 ToPixels(IntPtr hwnd, double widthDip, double heightDip)
    {
        var s = Scale(hwnd);
        return new Windows.Graphics.SizeInt32((int)Math.Round(widthDip * s), (int)Math.Round(heightDip * s));
    }

    /// <summary>A first-open size in DIPs, kept within 90% of the work area of the monitor the
    /// window is on (1280x800 DIPs at 150% is 1920x1200 pixels, taller than a 1080p screen).</summary>
    public static Windows.Graphics.SizeInt32 DefaultSize(IntPtr hwnd, AppWindow appWindow, double widthDip, double heightDip)
    {
        var size = ToPixels(hwnd, widthDip, heightDip);
        var area = DisplayArea.GetFromWindowId(appWindow.Id, DisplayAreaFallback.Primary).WorkArea;
        return new Windows.Graphics.SizeInt32(
            Math.Min(size.Width, (int)(area.Width * 0.9)), Math.Min(size.Height, (int)(area.Height * 0.9)));
    }

    /// <summary>Sets the minimum size in DIPs now, and re-applies it after a DPI change.</summary>
    public static void SetMinimum(Microsoft.UI.Xaml.Window window, AppWindow appWindow, double widthDip, double heightDip)
    {
        var hwnd = WinRT.Interop.WindowNative.GetWindowHandle(window);
        void Apply()
        {
            if (appWindow.Presenter is not OverlappedPresenter presenter) return;
            var min = ToPixels(hwnd, widthDip, heightDip);
            presenter.PreferredMinimumWidth = min.Width;
            presenter.PreferredMinimumHeight = min.Height;
        }
        Apply();
        var lastScale = Scale(hwnd);
        appWindow.Changed += (_, args) =>
        {
            if (!args.DidPositionChange && !args.DidSizeChange) return;
            var scale = Scale(hwnd);
            if (scale == lastScale) return;
            lastScale = scale;
            Apply();
        };
    }

    [DllImport("user32.dll")]
    private static extern uint GetDpiForWindow(IntPtr hwnd);
}
