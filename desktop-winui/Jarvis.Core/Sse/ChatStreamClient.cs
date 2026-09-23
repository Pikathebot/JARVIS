using System.Net.Http.Json;
using System.Text;
using System.Text.Json;
using Jarvis.Core.Api;
using Jarvis.Core.Json;
using Jarvis.Core.Models;

namespace Jarvis.Core.Sse;

public sealed class StreamChatOptions
{
    public required string Message { get; init; }
    public string? SessionId { get; init; }
    public string? ProjectId { get; init; }
    public string? Model { get; init; }

    /// <summary>"auto" | "normal" | "heavy"</summary>
    public string Mode { get; init; } = "auto";

    /// <summary>"WORKSPACE" | "FREEFORM" | "SYSTEM"</summary>
    public string ChatMode { get; init; } = "WORKSPACE";

    public bool Ephemeral { get; init; }

    public string? SystemPrompt { get; init; }
    public List<string>? ApprovedActionIds { get; init; }
    public List<Dictionary<string, object?>>? Attachments { get; init; }
    public required ChatStreamCallbacks Callbacks { get; init; }
}

/// <summary>Port of streamChat() in the retired Next.js client's src/lib/sse-client.ts: POST /chat/stream,
/// Accept: text/event-stream, dispatch typed events as they arrive.</summary>
public sealed class ChatStreamClient
{
    private readonly HttpClient _http;

    public ChatStreamClient(HttpClient http)
    {
        _http = http;
    }

    public async Task StreamChatAsync(StreamChatOptions options, CancellationToken ct = default)
    {
        var payload = new ChatRequest
        {
            Message = options.Message,
            SessionId = options.SessionId ?? "",
            ProjectId = options.ProjectId,
            Model = options.Model,
            Mode = options.Mode,
            ChatMode = options.ChatMode,
            SystemPrompt = options.SystemPrompt,
            ApprovedActionIds = options.ApprovedActionIds,
            Attachments = options.Attachments,
            Ephemeral = options.Ephemeral,
        };

        using var request = new HttpRequestMessage(HttpMethod.Post, "/chat/stream")
        {
            Content = JsonContent.Create(payload, options: JarvisJson.Options),
        };
        request.Headers.Accept.Add(new System.Net.Http.Headers.MediaTypeWithQualityHeaderValue("text/event-stream"));

        HttpResponseMessage response;
        try
        {
            response = await _http.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, ct).ConfigureAwait(false);
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            return; // matches signal?.aborted -> return in the TS client
        }

        if (!response.IsSuccessStatusCode)
        {
            var errText = await response.Content.ReadAsStringAsync(ct).ConfigureAwait(false);
            var message = $"HTTP {(int)response.StatusCode}: {errText}";
            options.Callbacks.OnError?.Invoke(new SseErrorEvent { Error = message });
            throw new ApiException(message, (int)response.StatusCode);
        }

        await using var stream = await response.Content.ReadAsStreamAsync(ct).ConfigureAwait(false);

        try
        {
            await foreach (var frame in SseReader.ReadFramesAsync(stream, ct).ConfigureAwait(false))
            {
                Dispatch(frame, options.Callbacks);
            }
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            // matches signal?.aborted -> return in the TS client
        }
        catch (Exception ex)
        {
            options.Callbacks.OnError?.Invoke(new SseErrorEvent { Error = ex.Message });
            throw;
        }
    }

    private static void Dispatch(SseFrame frame, ChatStreamCallbacks callbacks)
    {
        if (string.IsNullOrEmpty(frame.Data))
        {
            return;
        }

        try
        {
            switch (frame.EventType)
            {
                case "token":
                    callbacks.OnToken?.Invoke(Deserialize<SseTokenEvent>(frame.Data));
                    break;
                case "reasoning":
                    callbacks.OnReasoning?.Invoke(Deserialize<SseReasoningEvent>(frame.Data));
                    break;
                case "tool_draft":
                    callbacks.OnToolDraft?.Invoke(Deserialize<SseToolDraftEvent>(frame.Data));
                    break;
                case "tool_start":
                    callbacks.OnToolStart?.Invoke(Deserialize<SseToolStartEvent>(frame.Data));
                    break;
                case "tool_end":
                    callbacks.OnToolEnd?.Invoke(Deserialize<SseToolEndEvent>(frame.Data));
                    break;
                case "tool_call":
                    callbacks.OnToolCall?.Invoke(Deserialize<SseToolCallEvent>(frame.Data));
                    break;
                case "tool_result":
                    callbacks.OnToolResult?.Invoke(Deserialize<SseToolResultEvent>(frame.Data));
                    break;
                case "agent_status":
                    callbacks.OnAgentStatus?.Invoke(Deserialize<SseAgentStatusEvent>(frame.Data));
                    break;
                case "confirmation_required":
                    callbacks.OnConfirmationRequired?.Invoke(Deserialize<SseConfirmationRequiredEvent>(frame.Data));
                    break;
                case "retrieval_context":
                    callbacks.OnRetrievalContext?.Invoke(Deserialize<SseRetrievalContextEvent>(frame.Data));
                    break;
                case "file_change":
                    callbacks.OnFileChange?.Invoke(Deserialize<SseFileChangeEvent>(frame.Data));
                    break;
                case "artifact_update":
                    callbacks.OnArtifactUpdate?.Invoke(Deserialize<SseArtifactUpdateEvent>(frame.Data));
                    break;
                case "done":
                    callbacks.OnDone?.Invoke(Deserialize<SseDoneEvent>(frame.Data));
                    break;
                case "error":
                    callbacks.OnError?.Invoke(Deserialize<SseErrorEvent>(frame.Data));
                    break;
                default:
                    break;
            }
        }
        catch (JsonException)
        {
            // Matches the TS fallback: a non-JSON "token"/"error" payload is still delivered raw.
            if (frame.EventType == "token")
            {
                callbacks.OnToken?.Invoke(new SseTokenEvent { Delta = frame.Data });
            }
            else if (frame.EventType == "error")
            {
                callbacks.OnError?.Invoke(new SseErrorEvent { Error = frame.Data });
            }
        }
    }

    private static T Deserialize<T>(string json) =>
        JsonSerializer.Deserialize<T>(json, JarvisJson.Options)
        ?? throw new JsonException($"Null payload for {typeof(T).Name}");
}
