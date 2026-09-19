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
    public List<ToolStep> ToolSteps { get; set; } = new();
    public string? Model { get; set; }
    public string? Provider { get; set; }

    /// <summary>Why the router picked this model/provider for this turn (e.g. "Local execution
    /// routed to FAST_MODEL (Qwen3.5-4B-UD-Q4_K_XL)."). The only place this is visible today --
    /// there is no separate "which model will answer" indicator before sending.</summary>
    public string? RouteReason { get; set; }
    public List<Dictionary<string, object?>> ToolsUsed { get; set; } = new();
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

    [ObservableProperty]
    public partial bool IsStreaming { get; set; }

    public ChatMessage()
    {
        Content = "";
        ReasoningContent = "";
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

    /// <summary>"WORKSPACE" | "SYSTEM"</summary>
    [JsonPropertyName("chat_mode")]
    public string? ChatMode { get; set; }

    [JsonPropertyName("project_id")]
    public string? ProjectId { get; set; }

    public List<Dictionary<string, object?>>? Attachments { get; set; }
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
