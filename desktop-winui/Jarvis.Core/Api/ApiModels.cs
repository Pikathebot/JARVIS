using System.Text.Json.Serialization;

namespace Jarvis.Core.Api;

// Request-only payload shapes that don't have a corresponding response DTO in Models/.
// Mirrors the inline object literals in the retired Next.js client's src/lib/api.ts.

public sealed class CreateProjectRequest
{
    public string Name { get; set; } = "";
    public string? Description { get; set; }
    public string? Instructions { get; set; }

    [JsonPropertyName("workspace_path")]
    public string? WorkspacePath { get; set; }

    [JsonPropertyName("local_folders")]
    public List<string>? LocalFolders { get; set; }
}

public sealed class UpdateProjectRequest
{
    public string? Name { get; set; }
    public string? Description { get; set; }
    public string? Instructions { get; set; }

    [JsonPropertyName("workspace_path")]
    public string? WorkspacePath { get; set; }

    [JsonPropertyName("local_folders")]
    public List<string>? LocalFolders { get; set; }

    [JsonPropertyName("is_active")]
    public bool? IsActive { get; set; }
}

public sealed class CreateArtifactRequest
{
    public string Name { get; set; } = "";
    public string Type { get; set; } = "";
    public string Content { get; set; } = "";

    [JsonPropertyName("session_id")]
    public string? SessionId { get; set; }

    [JsonPropertyName("project_id")]
    public string? ProjectId { get; set; }

    public string? Summary { get; set; }
}

public sealed class UpdateArtifactRequest
{
    public string? Name { get; set; }
    public string? Type { get; set; }
    public string? Content { get; set; }
    public string? Summary { get; set; }

    [JsonPropertyName("create_new_version")]
    public bool? CreateNewVersion { get; set; }
}

public sealed class CreateArtifactVersionRequest
{
    public string Content { get; set; } = "";
    public string? Summary { get; set; }
}

public sealed class CreateMemoryRequest
{
    public string Category { get; set; } = "";
    public string Content { get; set; } = "";

    [JsonPropertyName("project_id")]
    public string? ProjectId { get; set; }

    [JsonPropertyName("source_session_id")]
    public string? SourceSessionId { get; set; }

    public double? Confidence { get; set; }
    public bool? Pinned { get; set; }
}

public sealed class VoiceStatusResponse
{
    [JsonPropertyName("wake_word_active")]
    public bool WakeWordActive { get; set; }

    [JsonPropertyName("wake_words")]
    public List<string> WakeWords { get; set; } = new();

    [JsonPropertyName("synthesizer_voice")]
    public string SynthesizerVoice { get; set; } = "";

    [JsonPropertyName("is_playing_audio")]
    public bool IsPlayingAudio { get; set; }
}

public sealed class ObservationsPage
{
    public List<Models.Observation> Observations { get; set; } = new();

    [JsonPropertyName("active_conditions")]
    public List<string> ActiveConditions { get; set; } = new();

    [JsonPropertyName("latest_seq")]
    public long LatestSeq { get; set; }
}

public sealed class AwarenessConfigPatch
{
    public bool? Enabled { get; set; }

    [JsonPropertyName("poll_seconds")]
    public double? PollSeconds { get; set; }

    [JsonPropertyName("restate_cooldown_seconds")]
    public double? RestateCooldownSeconds { get; set; }

    [JsonPropertyName("min_speak_severity")]
    public string? MinSpeakSeverity { get; set; }

    [JsonPropertyName("actions_enabled")]
    public bool? ActionsEnabled { get; set; }
}

public sealed class SendChatRequest
{
    public string Message { get; set; } = "";

    [JsonPropertyName("session_id")]
    public string SessionId { get; set; } = "";

    [JsonPropertyName("project_id")]
    public string? ProjectId { get; set; }

    /// <summary>"WORKSPACE" | "SYSTEM"</summary>
    [JsonPropertyName("chat_mode")]
    public string? ChatMode { get; set; }

    [JsonPropertyName("approved_action_ids")]
    public List<string>? ApprovedActionIds { get; set; }
}

public sealed class VoiceSayRequest
{
    public string Text { get; set; } = "";

    [JsonPropertyName("session_id")]
    public string SessionId { get; set; } = "";

    [JsonPropertyName("voice_id")]
    public string? VoiceId { get; set; }
}

public sealed class VoiceStateRequest
{
    public Models.VoiceState State { get; set; }
}

public sealed class GovernorPauseRequest
{
    public string Reason { get; set; } = "User requested manual pause";
}
