using Microsoft.UI;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Windows.Graphics;

namespace Jarvis_Glass;

/// <summary>
/// Tracks the live screen position of every registered top-level window, in raw display pixels, so
/// <see cref="LiquidGlassCanvas"/> can crop a live-capture frame (which spans a whole display) by
/// where the window *actually* is on screen rather than only where a panel sits within its own
/// window. Shaped like <see cref="LiveCaptureService"/>/<see cref="WallpaperBitmapCache"/>: one
/// shared static store, callers pull the latest value rather than having it pushed down through
/// every panel's dependency-property chain.
/// </summary>
public static class WindowPositionService
{
    private static readonly Dictionary<WindowId, RectInt32> _bounds = new();

    /// <summary>Raised whenever any tracked window's position or size changes, so panels can
    /// repaint their live-capture crop.</summary>
    public static event Action? Changed;

    /// <summary>
    /// Starts tracking a window's <see cref="AppWindow"/> position/size. Call once per top-level
    /// window (MainWindow, HudWindow) after construction. Safe to call more than once for the same
    /// window -- re-registering just replaces the subscription.
    /// </summary>
    public static void Register(Window window)
    {
        var hwnd = WinRT.Interop.WindowNative.GetWindowHandle(window);
        var windowId = Win32Interop.GetWindowIdFromWindow(hwnd);
        var appWindow = AppWindow.GetFromWindowId(windowId);

        _bounds[appWindow.Id] = appWindow.Position is { } pos
            ? new RectInt32(pos.X, pos.Y, appWindow.Size.Width, appWindow.Size.Height)
            : new RectInt32(0, 0, appWindow.Size.Width, appWindow.Size.Height);

        appWindow.Changed += (sender, args) =>
        {
            if (!args.DidPositionChange && !args.DidSizeChange) return;
            _bounds[sender.Id] = new RectInt32(sender.Position.X, sender.Position.Y, sender.Size.Width, sender.Size.Height);
            Changed?.Invoke();
        };
    }

    /// <summary>
    /// Resolves the window that hosts <paramref name="xamlRoot"/> and returns its last-known screen
    /// bounds in raw display pixels. False if the window was never <see cref="Register"/>ed --
    /// callers should fall back to a window-relative-only crop in that case.
    /// </summary>
    public static bool TryGetWindowBounds(XamlRoot xamlRoot, out RectInt32 boundsPx)
    {
        try
        {
            var windowId = xamlRoot.ContentIslandEnvironment.AppWindowId;
            return _bounds.TryGetValue(windowId, out boundsPx);
        }
        catch
        {
            // ContentIslandEnvironment can be unavailable in some hosting scenarios (e.g. designer);
            // treat that the same as "not registered" rather than crashing the draw pass.
            boundsPx = default;
            return false;
        }
    }
}
