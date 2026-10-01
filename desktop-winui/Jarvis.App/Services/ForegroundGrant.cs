using System.Runtime.InteropServices;

namespace Jarvis_App.Services;

/// <summary>
/// Lets the backend raise windows. Windows' foreground lock refuses SetForegroundWindow from a
/// background process, so focus_app (and launch_app on an app that is already open) only made
/// the window flash in the taskbar. The process the user is working in may pass that right on:
/// this is called when a turn starts, while this window is still the one the user acted in.
/// The grant lasts until the user's next input elsewhere, which covers the turn.
/// </summary>
public static class ForegroundGrant
{
    /// <summary>The backend's process id, from /health (0 until the first answer).</summary>
    public static int BackendPid { get; set; }

    public static void ToBackend()
    {
        if (BackendPid <= 0) return;
        try
        {
            AllowSetForegroundWindow((uint)BackendPid);
        }
        catch
        {
            // best effort: without it focus_app says Windows refused, as before
        }
    }

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool AllowSetForegroundWindow(uint dwProcessId);
}
