using Windows.Win32;
using Windows.Win32.Foundation;
using Windows.Win32.UI.WindowsAndMessaging;

namespace Jarvis_GlassLab;

/// <summary>
/// Excludes this window from every capture consumer (screen recorders, and critically our own
/// capture session) via SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE) - there is no "capture
/// the display except this one window" API, so instead we make DWM omit this HWND from every
/// capture pipeline while it still renders normally on the physical display. Ported from
/// desktop-winui/Jarvis.Glass/WindowCaptureExclusion.cs, simplified to a single window since
/// GlassLab has exactly one.
/// </summary>
internal static class WindowCaptureExclusion
{
    public static void SetExcluded(HWND hwnd, bool excluded)
    {
        PInvoke.SetWindowDisplayAffinity(
            hwnd,
            excluded ? WINDOW_DISPLAY_AFFINITY.WDA_EXCLUDEFROMCAPTURE : WINDOW_DISPLAY_AFFINITY.WDA_NONE);
    }
}
