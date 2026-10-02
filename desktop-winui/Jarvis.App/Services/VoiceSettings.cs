namespace Jarvis_App.Services;

/// <summary>
/// The two listening knobs on Settings > Voice, read by every <see cref="ViewModels.VoiceViewModel"/>
/// (main window and HUD) on each audio frame, and kept in LocalSettings. The defaults are the
/// values that were constants before (speech = 3x the measured room noise, 850 ms of silence
/// ends an utterance). Unpackaged runs have no LocalSettings; changes then last the session.
/// </summary>
public static class VoiceSettings
{
    public const float DefaultSpeechRatio = 3.0f;
    public const int DefaultPauseMs = 850;

    public const float MinSpeechRatio = 1.5f, MaxSpeechRatio = 6f;
    public const int MinPauseMs = 400, MaxPauseMs = 2000;

    /// <summary>How many times louder than the room's noise floor counts as speech. Lower is
    /// more sensitive (and more easily set off by noise).</summary>
    public static float SpeechRatio { get; private set; } = Load("VoiceSpeechRatio", DefaultSpeechRatio, MinSpeechRatio, MaxSpeechRatio);

    /// <summary>Silence that ends an utterance and sends it.</summary>
    public static int PauseMs { get; private set; } = (int)Load("VoicePauseMs", DefaultPauseMs, MinPauseMs, MaxPauseMs);

    public static void SetSpeechRatio(float value)
    {
        SpeechRatio = Math.Clamp(value, MinSpeechRatio, MaxSpeechRatio);
        Save("VoiceSpeechRatio", SpeechRatio);
    }

    public static void SetPauseMs(int value)
    {
        PauseMs = Math.Clamp(value, MinPauseMs, MaxPauseMs);
        Save("VoicePauseMs", PauseMs);
    }

    private static float Load(string key, float fallback, float min, float max)
    {
        try
        {
            return Windows.Storage.ApplicationData.Current.LocalSettings.Values[key] is double saved
                ? Math.Clamp((float)saved, min, max)
                : fallback;
        }
        catch
        {
            return fallback;
        }
    }

    private static void Save(string key, double value)
    {
        try
        {
            Windows.Storage.ApplicationData.Current.LocalSettings.Values[key] = value;
        }
        catch
        {
            // unpackaged: session only
        }
    }
}
