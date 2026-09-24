using System.Runtime.InteropServices;
using Jarvis_Glass;
using Microsoft.UI.Dispatching;

namespace Jarvis_App.Services;

/// <summary>
/// Lets a script screenshot the app -- the glass included -- for visual checks during
/// development. The windows are normally excluded from capture (WDA_EXCLUDEFROMCAPTURE), which
/// the live glass needs: a capturable window reads its own output back through the capture and
/// the renderer never settles (~70% GPU, and the governor then evicts the model). So exclusion is
/// only ever lifted while nothing renders:
/// <list type="number">
/// <item>the script creates <c>snapshot.request</c> beside the exe;</item>
/// <item>this freezes every glass host on its last frame and holds every spring animation
/// (<see cref="GlassHost.Frozen"/>, <see cref="GlassMotion.Paused"/>), lifts exclusion, and
/// writes <c>snapshot.ready</c> with each visible window's on-screen rect in physical pixels;</item>
/// <item>the script copies those pixels off the screen, then deletes <c>snapshot.request</c>;</item>
/// <item>this restores exclusion, waits for it to take effect, then resumes. It also resumes by
/// itself after <see cref="MaxHold"/>, so a crashed script can't leave the app capturable.</item>
/// </list>
/// scripts/snapshot-window.ps1 is the script side.
/// </summary>
public sealed class SnapshotService : IDisposable
{
    private static readonly TimeSpan MaxHold = TimeSpan.FromSeconds(10);

    private readonly DispatcherQueueTimer _timer;
    private readonly IReadOnlyList<(string Name, nint Hwnd)> _windows;
    private readonly string _requestPath = Path.Combine(AppContext.BaseDirectory, "snapshot.request");
    private readonly string _readyPath = Path.Combine(AppContext.BaseDirectory, "snapshot.ready");
    private DateTime _heldSince;
    private bool _holding;
    private bool _busy;

    public SnapshotService(DispatcherQueue dispatcher, IReadOnlyList<(string Name, nint Hwnd)> windows)
    {
        _windows = windows;
        TryDelete(_readyPath);
        // A stale request from a previous run must not freeze this one -- but a fresh one is a
        // script catching the startup sequence, and is answered.
        if (File.Exists(_requestPath) && DateTime.Now - File.GetLastWriteTime(_requestPath) > TimeSpan.FromSeconds(30))
        {
            TryDelete(_requestPath);
        }
        _timer = dispatcher.CreateTimer();
        _timer.Interval = TimeSpan.FromMilliseconds(250);
        _timer.Tick += async (_, _) => await TickAsync();
        _timer.Start();
    }

    private async Task TickAsync()
    {
        if (_busy) return;
        _busy = true;
        try
        {
            var requested = File.Exists(_requestPath);
            if (!_holding && requested) await HoldAsync();
            else if (_holding && (!requested || DateTime.UtcNow - _heldSince > MaxHold)) await ReleaseAsync();
        }
        catch (Exception ex)
        {
            App.Log($"snapshot: {ex.GetType().Name}: {ex.Message}");
        }
        finally
        {
            _busy = false;
        }
    }

    private async Task HoldAsync()
    {
        _holding = true;
        _heldSince = DateTime.UtcNow;
        GlassMotion.Paused = true;
        GlassHost.Frozen = true;
        // Let the last in-flight frame present before the window becomes capturable.
        await Task.Delay(50);
        // Only the windows on screen: a hidden one made capturable shows up in the capture as a
        // blank white slab of its size (the HUD did, over the sidebar).
        // And only a window nothing covers: the script copies screen pixels, so a window behind
        // another app would hand it that app's content instead (it happened once; see memory
        // winui_client). The main window must be the foreground window; the HUD is topmost.
        _lifted.Clear();
        var skipped = new List<string>();
        var foreground = GetForegroundWindow();
        foreach (var (name, hwnd) in _windows)
        {
            if (!IsWindowVisible(hwnd) || IsIconic(hwnd)) continue;
            if (name == "main" && hwnd != foreground)
            {
                skipped.Add($"# {name} skipped: not the foreground window");
                continue;
            }
            WindowCaptureExclusion.SetExcluded(hwnd, false);
            _lifted.Add(hwnd);
        }
        // DWM applies the affinity on its next composition; give it a few.
        await Task.Delay(150);

        var lines = new List<string>();
        foreach (var (name, hwnd) in _windows)
        {
            if (!_lifted.Contains(hwnd)) continue;
            if (DwmGetWindowAttribute(hwnd, DwmwaExtendedFrameBounds, out var r, Marshal.SizeOf<Rect>()) != 0) continue;
            lines.Add($"{name} {r.Left} {r.Top} {r.Right - r.Left} {r.Bottom - r.Top}");
        }
        lines.AddRange(skipped);
        File.WriteAllLines(_readyPath, lines);
        App.Log($"snapshot: holding ({string.Join("; ", lines)})");
    }

    private async Task ReleaseAsync()
    {
        TryDelete(_readyPath);
        RestoreExclusion();
        // Resume only once exclusion is back, or the first frames would refract the window itself.
        await Task.Delay(200);
        GlassHost.Frozen = false;
        GlassMotion.Paused = false;
        var timedOut = File.Exists(_requestPath);
        if (timedOut) TryDelete(_requestPath);
        _holding = false;
        App.Log(timedOut ? "snapshot: released after the hold limit" : "snapshot: released");
    }

    private readonly List<nint> _lifted = new();

    private void RestoreExclusion()
    {
        foreach (var hwnd in _lifted) WindowCaptureExclusion.SetExcluded(hwnd, true);
        _lifted.Clear();
    }

    private static void TryDelete(string path)
    {
        try { File.Delete(path); } catch { /* best effort */ }
    }

    public void Dispose()
    {
        _timer.Stop();
        if (_holding)
        {
            RestoreExclusion();
            GlassHost.Frozen = false;
            GlassMotion.Paused = false;
        }
        TryDelete(_readyPath);
    }

    private const int DwmwaExtendedFrameBounds = 9;

    [StructLayout(LayoutKind.Sequential)]
    private struct Rect { public int Left, Top, Right, Bottom; }

    [DllImport("dwmapi.dll")]
    private static extern int DwmGetWindowAttribute(nint hwnd, int attribute, out Rect value, int size);

    [DllImport("user32.dll")]
    private static extern bool IsWindowVisible(nint hwnd);

    [DllImport("user32.dll")]
    private static extern bool IsIconic(nint hwnd);

    [DllImport("user32.dll")]
    private static extern nint GetForegroundWindow();
}
