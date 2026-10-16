using System.Text.Json.Serialization;

namespace Jarvis.Core.Models;

public enum VoiceState
{
    Idle,
    Listening,
    Armed,
    Thinking,
    Speaking,
}

public sealed class VoiceSessionState
{
    [JsonPropertyName("session_id")]
    public string SessionId { get; set; } = "";

    public VoiceState State { get; set; }
    public bool Armed { get; set; }

    [JsonPropertyName("armed_seconds_remaining")]
    public double ArmedSecondsRemaining { get; set; }

    [JsonPropertyName("last_wake_word")]
    public string? LastWakeWord { get; set; }

    [JsonPropertyName("last_transcript")]
    public string LastTranscript { get; set; } = "";

    public int Turns { get; set; }
}

public sealed class VoiceListenResult
{
    [JsonPropertyName("should_respond")]
    public bool ShouldRespond { get; set; }

    public string Query { get; set; } = "";
    public string Transcript { get; set; } = "";

    [JsonPropertyName("wake_detected")]
    public bool WakeDetected { get; set; }

    [JsonPropertyName("wake_word")]
    public string? WakeWord { get; set; }

    public string Reason { get; set; } = "";

    [JsonPropertyName("speak_immediately")]
    public string SpeakImmediately { get; set; } = "";

    /// <summary>Voice mode: "chat" | "act" when Query should go to chat on the fast model; null
    /// when not in voice mode or the action already ran (then SpeakImmediately says so).</summary>
    [JsonPropertyName("voice_route")]
    public string? VoiceRoute { get; set; }

    public VoiceState State { get; set; }

    [JsonPropertyName("transcription_error")]
    public string? TranscriptionError { get; set; }

    [JsonPropertyName("duration_seconds")]
    public double? DurationSeconds { get; set; }

    public VoiceSessionState Session { get; set; } = new();
}

public sealed class VoiceSayResult
{
    [JsonPropertyName("spoken_text")]
    public string SpokenText { get; set; } = "";

    [JsonPropertyName("audio_base64")]
    public string AudioBase64 { get; set; } = "";

    [JsonPropertyName("audio_mime")]
    public string? AudioMime { get; set; }

    [JsonPropertyName("voice_id")]
    public string VoiceId { get; set; } = "";

    [JsonPropertyName("persona_id")]
    public string? PersonaId { get; set; }

    [JsonPropertyName("synthesis_failed")]
    public bool? SynthesisFailed { get; set; }

    public VoiceSessionState Session { get; set; } = new();
}

/// <summary>GET/POST /api/voice/mode.</summary>
public sealed class VoiceModeStatus
{
    public bool Enabled { get; set; }

    /// <summary>"enabling" | "disabling" while the model swap runs; "" otherwise.</summary>
    public string Busy { get; set; } = "";

    public string Error { get; set; } = "";

    [JsonPropertyName("loaded_model")]
    public string? LoadedModel { get; set; }
}

public sealed class HandsFreeStatus
{
    [JsonPropertyName("wake_words")]
    public List<string> WakeWords { get; set; } = new();

    [JsonPropertyName("follow_up_window_seconds")]
    public double FollowUpWindowSeconds { get; set; }

    public List<VoiceSessionState> Sessions { get; set; } = new();

    [JsonPropertyName("persona_id")]
    public string PersonaId { get; set; } = "";

    [JsonPropertyName("voice_id")]
    public string VoiceId { get; set; } = "";

    public string Greeting { get; set; } = "";

    [JsonPropertyName("available_voices")]
    public Dictionary<string, string> AvailableVoices { get; set; } = new();
}
