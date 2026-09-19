using Jarvis.Core.Models;

namespace Jarvis.Core.Sse;

/// <summary>Callback bag dispatched over as /chat/stream frames arrive. Mirrors SSEEventCallbacks
/// in the retired Next.js client's src/lib/types.ts. Handlers for tool_call/tool_result/agent_status/file_change/
/// artifact_update are included for forward-compat with backend/app/agent/loop.py, which is not on
/// the live path today but the client should not need changing when it is wired up.</summary>
public sealed class ChatStreamCallbacks
{
    public Action<SseTokenEvent>? OnToken { get; set; }
    public Action<SseReasoningEvent>? OnReasoning { get; set; }
    public Action<SseToolDraftEvent>? OnToolDraft { get; set; }
    public Action<SseToolStartEvent>? OnToolStart { get; set; }
    public Action<SseToolEndEvent>? OnToolEnd { get; set; }
    public Action<SseToolCallEvent>? OnToolCall { get; set; }
    public Action<SseToolResultEvent>? OnToolResult { get; set; }
    public Action<SseAgentStatusEvent>? OnAgentStatus { get; set; }
    public Action<SseConfirmationRequiredEvent>? OnConfirmationRequired { get; set; }
    public Action<SseRetrievalContextEvent>? OnRetrievalContext { get; set; }
    public Action<SseFileChangeEvent>? OnFileChange { get; set; }
    public Action<SseArtifactUpdateEvent>? OnArtifactUpdate { get; set; }
    public Action<SseDoneEvent>? OnDone { get; set; }
    public Action<SseErrorEvent>? OnError { get; set; }
}
