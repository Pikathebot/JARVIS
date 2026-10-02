using System.Text.Json;
using Jarvis_Glass;
using Microsoft.UI.Dispatching;

namespace Jarvis_App.Services;

/// <summary>
/// Keeps Settings > Developer glass tuning across restarts: the values that differ from the
/// code's defaults (<see cref="GlassTuning.Changes"/>) as one JSON string in LocalSettings,
/// applied at launch before any window publishes glass. Reset to defaults removes the entry.
/// Unpackaged runs have no LocalSettings; tuning then lasts for the session.
/// </summary>
public static class GlassTuningStore
{
    private const string SettingKey = "GlassTuning";
    private static DispatcherQueueTimer? _saveTimer;

    public static void Load()
    {
        try
        {
            if (Windows.Storage.ApplicationData.Current.LocalSettings.Values[SettingKey] is string json
                && JsonSerializer.Deserialize<Dictionary<string, float>>(json) is { Count: > 0 } saved)
            {
                GlassTuning.Apply(saved);
            }
        }
        catch
        {
            // a corrupt or missing entry just means defaults
        }
    }

    /// <summary>Saves once dragging pauses (a slider changes a value every frame).</summary>
    public static void SaveSoon()
    {
        if (_saveTimer is null)
        {
            var queue = DispatcherQueue.GetForCurrentThread();
            if (queue is null) { Save(); return; }
            _saveTimer = queue.CreateTimer();
            _saveTimer.Interval = TimeSpan.FromMilliseconds(400);
            _saveTimer.IsRepeating = false;
            _saveTimer.Tick += (_, _) => Save();
        }
        _saveTimer.Stop();
        _saveTimer.Start();
    }

    public static void Save()
    {
        try
        {
            var values = Windows.Storage.ApplicationData.Current.LocalSettings.Values;
            var changes = GlassTuning.Changes();
            if (changes.Count == 0) values.Remove(SettingKey);
            else values[SettingKey] = JsonSerializer.Serialize(changes);
        }
        catch
        {
            // unpackaged: session only
        }
    }
}
