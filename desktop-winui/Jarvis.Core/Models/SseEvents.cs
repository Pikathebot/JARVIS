using System.Text.Json.Serialization;

namespace Jarvis.Core.Models;

// ==========================================
// /chat/stream SSE event payloads
// ==========================================

public sealed class SseTokenEvent
{
    public string Delta { get; set; } = "";
}

/// <summary>A chunk of the model's reasoning/thinking output, structurally separate from the
/// answer text -- see backend/app/agent/llamacpp_provider.py's ThinkTagStreamScanner.</summary>
public sealed class SseReasoningEvent
{
    public string Delta { get; set; } = "";
}

public sealed class SseToolDraftEvent
{
    public string Tool { get; set; } = "";

    [JsonPropertyName("args_delta")]
    public string ArgsDelta { get; set; } = "";
}

public sealed class SseToolStartEvent
{
    public string Tool { get; set; } = "";
    public Dictionary<string, object?> Args { get; set; } = new();
}

public sealed class SseToolEndEvent
{
    public string Tool { get; set; } = "";
    public Dictionary<string, object?> Args { get; set; } = new();

    /// <summary>"success" | "error"</summary>
    public string Status { get; set; } = "";

    public object? Result { get; set; }
}

public sealed class SseToolCallEvent
{
    public string Tool { get; set; } = "";
    public Dictionary<string, object?> Args { get; set; } = new();

    [JsonPropertyName("call_id")]
    public string? CallId { get; set; }
}

public sealed class SseToolResultEvent
{
    public string Tool { get; set; } = "";
    public string Status { get; set; } = "";
    public string? Summary { get; set; }
    public string? Result { get; set; }

    [JsonPropertyName("call_id")]
    public string? CallId { get; set; }

    [JsonPropertyName("latency_ms")]
    public double? LatencyMs { get; set; }

    public bool? Truncated { get; set; }
}

public sealed class SseAgentStatusEvent
{
    public string Status { get; set; } = "";
    public int? Iteration { get; set; }

    [JsonPropertyName("run_id")]
    public string? RunId { get; set; }

    public string? Tool { get; set; }
}

public sealed class SseConfirmationRequiredEvent
{
    [JsonPropertyName("pending_confirmations")]
    public List<PendingConfirmation> PendingConfirmations { get; set; } = new();

    [JsonPropertyName("session_id")]
    public string SessionId { get; set; } = "";

    public string? Response { get; set; }
    public string? Spoken { get; set; }
}

public sealed class RetrievedChunk
{
    [JsonPropertyName("chunk_id")]
    public string? ChunkId { get; set; }

    [JsonPropertyName("file_path")]
    public string? FilePath { get; set; }

    [JsonPropertyName("file_name")]
    public string? FileName { get; set; }

    [JsonPropertyName("symbol_name")]
    public string? SymbolName { get; set; }

    [JsonPropertyName("symbol_type")]
    public string? SymbolType { get; set; }

    [JsonPropertyName("start_line")]
    public int? StartLine { get; set; }

    [JsonPropertyName("end_line")]
    public int? EndLine { get; set; }

    public string? Content { get; set; }
    public double? Score { get; set; }

    [JsonPropertyName("similarity_score")]
    public double? SimilarityScore { get; set; }

    [JsonPropertyName("rrf_score")]
    public double? RrfScore { get; set; }
}

public sealed class BudgetReport
{
    [JsonPropertyName("total_context_window")]
    public int TotalContextWindow { get; set; }

    [JsonPropertyName("reserved_output_tokens")]
    public int ReservedOutputTokens { get; set; }

    [JsonPropertyName("available_input_budget")]
    public int AvailableInputBudget { get; set; }

    [JsonPropertyName("tier1_system_tokens")]
    public int Tier1SystemTokens { get; set; }

    [JsonPropertyName("tier2_user_tokens")]
    public int Tier2UserTokens { get; set; }

    [JsonPropertyName("tier3_rag_tokens")]
    public int Tier3RagTokens { get; set; }

    [JsonPropertyName("tier4_history_tokens")]
    public int Tier4HistoryTokens { get; set; }

    [JsonPropertyName("tier5_summary_included")]
    public bool Tier5SummaryIncluded { get; set; }

    [JsonPropertyName("total_input_tokens_used")]
    public int TotalInputTokensUsed { get; set; }

    [JsonPropertyName("remaining_unallocated_tokens")]
    public int RemainingUnallocatedTokens { get; set; }

    [JsonPropertyName("chunks_used_count")]
    public int ChunksUsedCount { get; set; }

    [JsonPropertyName("chunks_dropped_count")]
    public int ChunksDroppedCount { get; set; }

    [JsonPropertyName("chat_mode")]
    public string ChatMode { get; set; } = "";
}

public sealed class SseRetrievalContextEvent
{
    [JsonPropertyName("chunks_used")]
    public List<RetrievedChunk> ChunksUsed { get; set; } = new();

    [JsonPropertyName("chunks_dropped")]
    public List<RetrievedChunk> ChunksDropped { get; set; } = new();

    [JsonPropertyName("budget_report")]
    public BudgetReport BudgetReport { get; set; } = new();
}

public sealed class SseDoneEvent
{
    public string Response { get; set; } = "";

    /// <summary>The turn's full accumulated reasoning/thinking text, if the model produced any.</summary>
    public string? Reasoning { get; set; }

    public string Model { get; set; } = "";
    public string Provider { get; set; } = "";
    public string Status { get; set; } = "";

    [JsonPropertyName("session_id")]
    public string? SessionId { get; set; }

    [JsonPropertyName("route_reason")]
    public string? RouteReason { get; set; }

    [JsonPropertyName("fallback_used")]
    public bool? FallbackUsed { get; set; }

    [JsonPropertyName("compaction_performed")]
    public bool? CompactionPerformed { get; set; }

    [JsonPropertyName("active_skills")]
    public List<string> ActiveSkills { get; set; } = new();

    [JsonPropertyName("tools_used")]
    public List<Dictionary<string, object?>> ToolsUsed { get; set; } = new();
}

public sealed class SseErrorEvent
{
    public string Error { get; set; } = "";
}

/// <summary>Emitted by AgentLoop but not on the live /chat/stream path today (see backend/app/agent/loop.py). Handled for forward-compat.</summary>
public sealed class SseFileChangeEvent
{
    public string Path { get; set; } = "";
    public string ChangeType { get; set; } = "";
}

/// <summary>Emitted by AgentLoop but not on the live /chat/stream path today. Handled for forward-compat.</summary>
public sealed class SseArtifactUpdateEvent
{
    [JsonPropertyName("artifact_id")]
    public string ArtifactId { get; set; } = "";
}

/// <summary>A single line item in the Activity tab's agent trace.</summary>
public sealed class ActivityStep
{
    public string Id { get; set; } = Guid.NewGuid().ToString("n");
    public DateTimeOffset Timestamp { get; set; } = DateTimeOffset.Now;

    /// <summary>"status" | "tool_call" | "tool_result"</summary>
    public string Type { get; set; } = "";

    public string? Tool { get; set; }
    public string? Status { get; set; }
    public Dictionary<string, object?>? Args { get; set; }
    public string? Result { get; set; }
    public string? Summary { get; set; }
    public double? LatencyMs { get; set; }
    public bool? Truncated { get; set; }
}
