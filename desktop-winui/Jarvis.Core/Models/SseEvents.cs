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

/// <summary>The conversation was compacted (PLAN 4.25): the model goes on from this summary and the
/// last exchanges. <see cref="Manual"/> = asked for with /compact; otherwise it ran after the last turn.</summary>
public sealed class SseCompactedEvent
{
    public string Summary { get; set; } = "";
    public double At { get; set; }
    public bool Manual { get; set; }
}

/// <summary>A few words on what the model is doing while it thinks ("Checking the calendar for
/// Friday"), from llama-server's second slot -- backend/app/agent/thinking_narrator.py.</summary>
public sealed class SseThinkingStatusEvent
{
    public string Text { get; set; } = "";
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
    public string? Model { get; set; }
    public string? Provider { get; set; }
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

    /// <summary>True when the streamed text was a tool call written out on a turn with no tools:
    /// the bubble shows <see cref="Response"/> in place of what streamed.</summary>
    [JsonPropertyName("replace_content")]
    public bool ReplaceContent { get; set; }

    /// <summary>The turn's numbers for the reply's metrics line (PLAN 4.13 #9); null from paths
    /// that don't report them (cloud, the duplicate-loop stop).</summary>
    public TurnMetrics? Metrics { get; set; }

    /// <summary>Set now and then on a text-only reply (PLAN 4.13 #8): a second take may be on its
    /// way; poll /api/compare/{id} and show a compare card only if one becomes ready.</summary>
    [JsonPropertyName("compare_id")]
    public string? CompareId { get; set; }
}

/// <summary>What one reply cost: first-token time and prompt/cache sizes from the first model round,
/// generation speed over all rounds, and the rounds, tools and backend safety nets it needed.</summary>
public sealed class TurnMetrics
{
    public int Rounds { get; set; }
    public int Tools { get; set; }
    public double? TtftMs { get; set; }
    public double? TokensPerSecond { get; set; }
    public int? PromptTokens { get; set; }
    public int? CachedTokens { get; set; }
    public List<string> Nets { get; set; } = new();

    /// <summary>The footer line, e.g. "main · 0.4 s first token · 27 tok/s · 3 rounds · 2 tools ·
    /// 1.6k prompt (1.4k cached) · nudged". Parts the turn didn't produce are left out.</summary>
    public static string Format(string? model, TurnMetrics? m)
    {
        var parts = new List<string>();
        if (!string.IsNullOrEmpty(model)) parts.Add(model);
        if (m is not null)
        {
            var inv = System.Globalization.CultureInfo.InvariantCulture;
            if (m.TtftMs is double t) parts.Add((t / 1000.0).ToString("0.0", inv) + " s first token");
            if (m.TokensPerSecond is double s) parts.Add(Math.Round(s).ToString(inv) + " tok/s");
            if (m.Rounds > 1) parts.Add($"{m.Rounds} rounds");
            if (m.Tools > 0) parts.Add(m.Tools == 1 ? "1 tool" : $"{m.Tools} tools");
            if (m.PromptTokens is int p)
                parts.Add(Kilo(p, inv) + " prompt" + (m.CachedTokens is int c && c > 0 ? $" ({Kilo(c, inv)} cached)" : ""));
            foreach (var net in m.Nets.Distinct())
            {
                var n = m.Nets.Count(x => x == net);
                parts.Add(n > 1 ? $"{net} ×{n}" : net);
            }
        }
        return string.Join(" · ", parts);
    }

    private static string Kilo(int tokens, IFormatProvider inv) =>
        tokens >= 1000 ? (tokens / 1000.0).ToString("0.0", inv) + "k" : tokens.ToString(inv);
}

public sealed class SseErrorEvent
{
    public string Error { get; set; } = "";
}

/// <summary>The Freeform thread (session "jarvis-thread", PLAN 4.18): this turn opened a new
/// episode (a 3-hour gap or New topic) or folded the current one.</summary>
public sealed class SseEpisodeEvent
{
    public long Id { get; set; }
    public bool Opened { get; set; }
    public bool Folded { get; set; }
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
