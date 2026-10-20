using System.Text.Json.Serialization;
using CommunityToolkit.Mvvm.ComponentModel;

namespace Jarvis.Core.Models;

public enum MessageRole
{
    User,
    Assistant,
    System,
    Tool,
}

/// <summary>What a non-message row of the Freeform thread is (PLAN 4.18).</summary>
public enum ThreadMark
{
    None,
    Day,
    Episode,
    Digest,
}

public enum ToolStatus
{
    Running,
    Success,
    Error,
}

public sealed class ToolStep
{
    public string Id { get; set; } = "";
    public string Tool { get; set; } = "";
    public Dictionary<string, object?> Args { get; set; } = new();
    public ToolStatus Status { get; set; }
    public object? Result { get; set; }

    /// <summary>Raw, not-yet-valid-JSON argument text streamed via tool_draft.</summary>
    public string? RawArgs { get; set; }

    /// <summary>What the chat line says after its Undo was used (remember/forget lines), kept on
    /// the step so a re-render does not offer the Undo again.</summary>
    public string? UndoneText { get; set; }
}

public sealed class PendingConfirmation
{
    [JsonPropertyName("action_id")]
    public string ActionId { get; set; } = "";

    public string Tool { get; set; } = "";

    /// <summary>Backend sends either an object or a pre-formatted string.</summary>
    [JsonPropertyName("args")]
    public System.Text.Json.JsonElement ArgsRaw { get; set; }

    [JsonPropertyName("risk_tier")]
    public string? RiskTier { get; set; }

    public string? Reason { get; set; }
}

/// <summary>A single turn in the chat transcript. Client-side model, not sent to the backend as-is.
/// ObservableObject (not a plain class) because Content mutates token-by-token while streaming and
/// the message list binds to it with x:Bind Mode=OneWay — without change notification the visible
/// text never updates as tokens arrive.</summary>
public partial class ChatMessage : ObservableObject
{
    public string Id { get; set; } = Guid.NewGuid().ToString("n");
    public MessageRole Role { get; set; }

    [ObservableProperty]
    public partial string Content { get; set; }

    /// <summary>Reasoning/thinking text routed through the structured "reasoning" SSE channel,
    /// kept separate from <see cref="Content"/> so it can render in its own panel rather than an
    /// inline block. Empty for legacy messages -- see <see cref="HasStructuredReasoning"/>.</summary>
    [ObservableProperty]
    public partial string ReasoningContent { get; set; }

    /// <summary>True for any message populated via the structured reasoning channel (live-streamed
    /// post-feature, or reloaded with a non-null reasoning_content DB column) -- false for messages
    /// stored before this feature shipped, whose reasoning (if any) is still baked into
    /// &lt;think&gt; tags inside <see cref="Content"/> and must render via the legacy inline path.</summary>
    public bool HasStructuredReasoning { get; set; }

    public DateTimeOffset CreatedAt { get; set; } = DateTimeOffset.Now;

    /// <summary>The Freeform thread (PLAN 4.18): not a message but a mark in the transcript --
    /// a day divider, an episode marker or an episode's digest card. <see cref="MarkText"/> is
    /// its line, <see cref="MarkDetail"/> what the digest card opens to.</summary>
    public ThreadMark Mark { get; set; }

    [ObservableProperty]
    public partial string MarkText { get; set; }

    public string MarkDetail { get; set; } = "";

    /// <summary>The backend's message id (thread history only: the scroll-back pages by it).</summary>
    public long? StoredId { get; set; }

    /// <summary>The thread episode an episode marker or digest card belongs to.</summary>
    public long? EpisodeId { get; set; }

    public List<ToolStep> ToolSteps { get; set; } = new();
    public string? Model { get; set; }
    public string? Provider { get; set; }

    /// <summary>Why the router picked this model/provider for this turn (e.g. "Local execution
    /// routed to FAST_MODEL (Qwen3.5-4B-UD-Q4_K_XL)."). The only place this is visible today --
    /// there is no separate "which model will answer" indicator before sending.</summary>
    public string? RouteReason { get; set; }
    public List<Dictionary<string, object?>> ToolsUsed { get; set; } = new();

    /// <summary>The live turn's numbers for the metrics footer; null on history reloaded from the backend.</summary>
    public TurnMetrics? Metrics { get; set; }
    public List<string> ActiveSkills { get; set; } = new();
    public List<PendingConfirmation> PendingConfirmations { get; set; } = new();

    /// <summary>Removes a decided confirmation and announces the change, so the bubble showing
    /// its card re-renders and drops the approve/deny buttons for it.</summary>
    public void ResolveConfirmation(PendingConfirmation confirmation)
    {
        if (PendingConfirmations.Remove(confirmation))
        {
            OnPropertyChanged(nameof(PendingConfirmations));
        }
    }

    /// <summary>TTS-ready text for this message, when it differs from the displayed content.</summary>
    public string? Spoken { get; set; }

    /// <summary>The in-app compare for this reply (PLAN 4.13 #8): its id from the done event, then the
    /// second take once it is ready. The card shows while <see cref="CompareAlternative"/> is set and
    /// no vote was cast.</summary>
    public string? CompareId { get; set; }

    [ObservableProperty]
    public partial string? CompareAlternative { get; set; }

    /// <summary>How the second take differs ("shorter", "more casual", ...).</summary>
    public string? CompareAxis { get; set; }

    [ObservableProperty]
    public partial bool CompareVoted { get; set; }

    [ObservableProperty]
    public partial bool IsStreaming { get; set; }

    public ChatMessage()
    {
        Content = "";
        ReasoningContent = "";
        MarkText = "";
    }
}

public sealed class ChatRequest
{
    public string Message { get; set; } = "";

    [JsonPropertyName("session_id")]
    public string SessionId { get; set; } = "";

    public string? Model { get; set; }

    /// <summary>"auto" | "normal" | "heavy"</summary>
    public string Mode { get; set; } = "auto";

    [JsonPropertyName("system_prompt")]
    public string? SystemPrompt { get; set; }

    [JsonPropertyName("approved_action_ids")]
    public List<string>? ApprovedActionIds { get; set; }

    /// <summary>"WORKSPACE" | "FREEFORM" | "SYSTEM"</summary>
    [JsonPropertyName("chat_mode")]
    public string? ChatMode { get; set; }

    [JsonPropertyName("project_id")]
    public string? ProjectId { get; set; }

    public List<Dictionary<string, object?>>? Attachments { get; set; }

    /// <summary>Scratchpad turn: the backend keeps the conversation in memory only and writes
    /// nothing to its database or session list.</summary>
    public bool Ephemeral { get; set; }

    /// <summary>Voice mode's route for this turn ("chat" | "act"), from /api/voice/listen; null
    /// for a typed turn.</summary>
    [JsonPropertyName("voice_route")]
    public string? VoiceRoute { get; set; }

    /// <summary>The Freeform thread only: start a new episode with this message (the New topic
    /// button, PLAN 4.18).</summary>
    [JsonPropertyName("new_topic")]
    public bool NewTopic { get; set; }
}

public sealed class ChatResponse
{
    public string Response { get; set; } = "";
    public string Model { get; set; } = "";
    public string Provider { get; set; } = "";
    public string Status { get; set; } = "";

    [JsonPropertyName("session_id")]
    public string SessionId { get; set; } = "";

    [JsonPropertyName("pending_confirmations")]
    public List<PendingConfirmation>? PendingConfirmations { get; set; }

    public string? Spoken { get; set; }
}
