using System.Runtime.InteropServices;
using Microsoft.UI.Xaml;

namespace Jarvis_Glass;

/// <summary>
/// Excludes (or restores) Jarvis's own top-level windows from every system capture consumer via
/// <c>SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)</c>. This is what actually fixes live
/// capture's feedback loop: <see cref="LiveCaptureSource"/> captures the final composited screen,
/// which otherwise always includes this app's own opaque glass panel drawn on top of wherever it
/// sits -- there is no "capture the display except this window" mode in
/// Windows.Graphics.Capture. Excluding the window from capture makes DWM omit it from every
/// capture pipeline (ours included) while still rendering it normally on the physical display, so
/// our own capture session sees genuinely whatever is behind it instead of its own last frame.
///
/// The trade-off is systemwide, not scoped to our own capture session: while excluded, Jarvis is
/// also invisible to screen recorders, Zoom/Teams screen-share, and Xbox Game Bar. That is
/// unavoidable with this API -- there is no "exclude from capture except for this one caller"
/// variant. Settings surfaces this to the user next to the Live-capture toggle rather than the app
/// silently doing something a screen-share participant would find surprising.
/// </summary>
public static class WindowCaptureExclusion
{
    private const uint WdaNone = 0x0;
    private const uint WdaExcludeFromCapture = 0x11;

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool SetWindowDisplayAffinity(IntPtr hWnd, uint dwAffinity);

    private static readonly List<IntPtr> _hwnds = new();

    /// <summary>Call once per top-level window (MainWindow, HudWindow) after construction, same as
    /// <see cref="WindowPositionService.Register"/>.</summary>
    public static void Register(Window window) => Register(WinRT.Interop.WindowNative.GetWindowHandle(window));

    /// <summary>Raw-HWND form for the glass backdrop windows <see cref="GlassHost"/> creates.</summary>
    public static void Register(IntPtr hwnd)
    {
        if (!_hwnds.Contains(hwnd))
        {
            _hwnds.Add(hwnd);
        }
    }

    /// <summary>
    /// Applies or clears capture exclusion on every registered window. Safe to call redundantly --
    /// <see cref="GlassHost"/> only calls this once capture is up, but nothing
    /// here depends on that. Requires Windows 10 2004+ / Windows 11; on older builds
    /// SetWindowDisplayAffinity simply fails (returns false, no exception) and this silently no-ops.
    /// </summary>
    public static void SetExcluded(bool excluded)
    {
        var affinity = excluded ? WdaExcludeFromCapture : WdaNone;
        foreach (var hwnd in _hwnds)
        {
            SetWindowDisplayAffinity(hwnd, affinity);
        }
    }
}
