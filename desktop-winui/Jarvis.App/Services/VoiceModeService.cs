using System;
using System.Threading.Tasks;
using Jarvis.Core.Api;
using Jarvis.Core.Models;

namespace Jarvis_App.Services;

/// <summary>
/// Voice mode (PLAN 4.9) as one switch the main window and the HUD share: the backend swaps the
/// 9B out for MiniCPM and starts Laya and GPU Whisper, which takes ~45-65 s, so the state
/// includes "switching".
/// Both windows listen to <see cref="Changed"/> so their buttons never disagree.
/// </summary>
public static class VoiceModeService
{
    public static bool Enabled { get; private set; }

    /// <summary>True while the backend is swapping models; the buttons are disabled meanwhile.</summary>
    public static bool Switching { get; private set; }

    /// <summary>The backend's last complaint (fast model didn't load, Laya didn't start); "" if none.</summary>
    public static string Error { get; private set; } = "";

    public static event Action? Changed;

    private static readonly TimeSpan SwitchLimit = TimeSpan.FromMinutes(10);

    public static async Task RefreshAsync(JarvisApiClient api)
    {
        try
        {
            Apply(await api.FetchVoiceModeAsync().ConfigureAwait(false));
        }
        catch (Exception ex)
        {
            App.LogVoice($"voice mode status failed: {ex.Message}");
        }
    }

    public static async Task SetAsync(JarvisApiClient api, bool enabled)
    {
        if (Switching) return;
        Switching = true;
        Error = "";
        Changed?.Invoke();
        try
        {
            App.LogVoice($"voice mode -> {(enabled ? "on" : "off")}");
            // The backend answers at once and swaps in the background: a switch takes ~45-65 s, and
            // minutes when Whisper's GPU model downloads. Waiting on the POST itself hit the
            // client's 30 s timeout every first time (2026-10-04, 2026-10-14).
            var status = await api.SetVoiceModeAsync(enabled).ConfigureAwait(false);
            var started = DateTime.UtcNow;
            while (!string.IsNullOrEmpty(status.Busy) && DateTime.UtcNow - started < SwitchLimit)
            {
                await Task.Delay(500).ConfigureAwait(false);
                status = await api.FetchVoiceModeAsync().ConfigureAwait(false);
            }
            if (!string.IsNullOrEmpty(status.Busy))
                status.Error = $"Voice mode is still {status.Busy} after {SwitchLimit.TotalMinutes:0} minutes.";
            App.LogVoice($"voice mode is {(status.Enabled ? "on" : "off")} after {(DateTime.UtcNow - started).TotalSeconds:0} s"
                         + (string.IsNullOrEmpty(status.Error) ? "" : $": {status.Error}"));
            Apply(status);
        }
        catch (Exception ex)
        {
            Error = ex.Message;
            App.LogVoice($"voice mode switch failed: {ex.Message}");
        }
        finally
        {
            Switching = false;
            Changed?.Invoke();
        }
    }

    private static void Apply(VoiceModeStatus status)
    {
        Enabled = status.Enabled;
        Error = status.Error ?? "";
        Changed?.Invoke();
    }
}
