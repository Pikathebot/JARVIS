using System.Collections.ObjectModel;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using Jarvis.Core.Api;
using Jarvis.Core.Models;
using Jarvis.Core.Sse;
using Microsoft.UI.Dispatching;

namespace Jarvis_App.ViewModels;

/// <summary>
/// Port of desktop-app/src/hooks/useChat.ts. Owns the transcript for one chat session and drives
/// POST /chat/stream via ChatStreamClient. UI-thread marshalling happens through the supplied
/// DispatcherQueue since SSE callbacks arrive on a background thread.
/// </summary>
public partial class ChatViewModel : ObservableObject
{
    private readonly JarvisApiClient _api;
    private readonly ChatStreamClient _streamClient;
    private readonly DispatcherQueue _dispatcher;

    private CancellationTokenSource? _abort;
    private string _lastUserPrompt = "";
    private ChatMessage? _streamingMessage;

    public ObservableCollection<ChatMessage> Messages { get; } = new();

    [ObservableProperty]
    public partial string ActiveSessionId { get; set; } = JarvisApiClient.CreateNewSessionId();

    [ObservableProperty]
    public partial bool IsLoading { get; set; }

    [ObservableProperty]
    public partial string? Error { get; set; }

    [ObservableProperty]
    public partial string? ProjectId { get; set; }

    /// <summary>"WORKSPACE" | "SYSTEM"</summary>
    [ObservableProperty]
    public partial string ChatMode { get; set; } = "WORKSPACE";

    public ObservableCollection<PendingConfirmation> PendingConfirmations { get; } = new();

    /// <summary>Powers the right panel's Context tab: token budget + retrieved chunks from the
    /// most recent turn's retrieval_context event.</summary>
    [ObservableProperty]
    public partial SseRetrievalContextEvent? LatestRetrieval { get; set; }

    /// <summary>Powers the right panel's Activity tab: a running trace of tool calls for the
    /// active session (not cleared between turns, only on NewChat/LoadSession).</summary>
    public ObservableCollection<ActivityStep> ActivitySteps { get; } = new();

    /// <summary>Powers the right panel's Reasoning tab: whichever assistant message is currently
    /// streaming, or the most recently completed one otherwise. Never auto-opens the panel --
    /// callers only use this to render content into a tab the user may or may not have open, and
    /// <see cref="IsReasoningStreaming"/> to drive a passive badge on that tab's header.</summary>
    [ObservableProperty]
    public partial ChatMessage? ActiveReasoningMessage { get; set; }

    [ObservableProperty]
    public partial bool IsReasoningStreaming { get; set; }

    public event Action<ChatMessage>? MessageCompleted;

    public ChatViewModel(JarvisApiClient api, ChatStreamClient streamClient, DispatcherQueue dispatcher)
    {
        _api = api;
        _streamClient = streamClient;
        _dispatcher = dispatcher;
    }

    public async Task SendMessageAsync(string content, List<Attachment>? attachments = null)
    {
        if (string.IsNullOrWhiteSpace(content) && PendingConfirmations.Count == 0 && (attachments is null || attachments.Count == 0))
        {
            return;
        }

        if (!string.IsNullOrWhiteSpace(content) || (attachments is { Count: > 0 }))
        {
            var displayContent = content;
            if (string.IsNullOrWhiteSpace(displayContent) && attachments is { Count: > 0 })
            {
                displayContent = $"Uploaded {attachments.Count} file(s): {string.Join(", ", attachments.Select(a => a.Filename))}";
            }
            _lastUserPrompt = displayContent;
            Messages.Add(new ChatMessage { Role = MessageRole.User, Content = displayContent });
        }

        var approvedIds = PendingConfirmations.Select(p => p.ActionId).ToList();
        PendingConfirmations.Clear();

        var attachmentPayload = attachments?.Select(a => new Dictionary<string, object?>
        {
            ["id"] = a.Id,
            ["filename"] = a.Filename,
            ["path"] = a.Path,
        }).ToList();

        await RunStreamAsync(content, approvedIds, attachmentPayload).ConfigureAwait(false);
    }

    [RelayCommand]
    public Task ConfirmActionAsync(PendingConfirmation confirmation)
    {
        PendingConfirmations.Remove(confirmation);
        return RunStreamAsync("", new List<string> { confirmation.ActionId });
    }

    [RelayCommand]
    public void DenyAction(PendingConfirmation confirmation)
    {
        // Client-only today, matching useChat.ts's denyAction — the backend is never told.
        // See PLAN.md's existing "Confirmation timeout voice feedback" milestone for the real fix.
        PendingConfirmations.Remove(confirmation);
        Messages.Add(new ChatMessage
        {
            Role = MessageRole.System,
            Content = "Action execution was denied by user.",
        });
    }

    [RelayCommand]
    public void AbortStream()
    {
        _abort?.Cancel();
    }

    public void NewChat()
    {
        _abort?.Cancel();
        Messages.Clear();
        PendingConfirmations.Clear();
        ActivitySteps.Clear();
        LatestRetrieval = null;
        ActiveReasoningMessage = null;
        IsReasoningStreaming = false;
        ActiveSessionId = JarvisApiClient.CreateNewSessionId();
        Error = null;
    }

    /// <summary>Switches to an existing session and loads its history. The backend returns raw
    /// message dicts (its persisted schema, not the SSE contract), so fields are read
    /// defensively — anything missing just renders as an empty string rather than throwing.</summary>
    public async Task LoadSessionAsync(string sessionId)
    {
        _abort?.Cancel();
        Messages.Clear();
        PendingConfirmations.Clear();
        ActivitySteps.Clear();
        LatestRetrieval = null;
        ActiveReasoningMessage = null;
        IsReasoningStreaming = false;
        Error = null;
        ActiveSessionId = sessionId;

        try
        {
            var raw = await _api.FetchSessionMessagesAsync(sessionId).ConfigureAwait(false);
            foreach (var entry in raw)
            {
                var msg = MapRawMessage(entry);
                Messages.Add(msg);
                if (msg.Role == MessageRole.Assistant)
                {
                    ActiveReasoningMessage = msg;
                }
            }
        }
        catch (Exception ex)
        {
            Error = ex.Message;
        }
    }

    private static ChatMessage MapRawMessage(Dictionary<string, object?> entry)
    {
        var role = ReadString(entry, "role") switch
        {
            "user" => MessageRole.User,
            "system" => MessageRole.System,
            "tool" => MessageRole.Tool,
            _ => MessageRole.Assistant,
        };
        var reasoning = ReadString(entry, "reasoning_content");
        return new ChatMessage
        {
            Role = role,
            Content = ReadString(entry, "content") ?? "",
            ReasoningContent = reasoning ?? "",
            // A null/absent column means this row predates the structured-reasoning feature --
            // any reasoning it has is still baked into <think> tags inside Content and must render
            // through the legacy inline Expander path, not this one.
            HasStructuredReasoning = !string.IsNullOrEmpty(reasoning),
            Model = ReadString(entry, "model"),
            Provider = ReadString(entry, "provider"),
        };
    }

    private static string? ReadString(Dictionary<string, object?> entry, string key)
    {
        if (!entry.TryGetValue(key, out var value) || value is null) return null;
        return value switch
        {
            System.Text.Json.JsonElement je when je.ValueKind == System.Text.Json.JsonValueKind.String => je.GetString(),
            string s => s,
            _ => value.ToString(),
        };
    }

    private async Task RunStreamAsync(string message, List<string> approvedActionIds, List<Dictionary<string, object?>>? attachments = null)
    {
        _abort?.Cancel();
        _abort = new CancellationTokenSource();
        var ct = _abort.Token;

        IsLoading = true;
        Error = null;

        _streamingMessage = new ChatMessage { Role = MessageRole.Assistant, IsStreaming = true, HasStructuredReasoning = true };
        Messages.Add(_streamingMessage);
        ActiveReasoningMessage = _streamingMessage;
        IsReasoningStreaming = false;

        var callbacks = new ChatStreamCallbacks
        {
            OnToken = e => Post(() => AppendToken(e.Delta)),
            OnReasoning = e => Post(() => AppendReasoning(e.Delta)),
            OnToolStart = e => Post(() => AddToolStep(e.Tool, e.Args, ToolStatus.Running)),
            OnToolEnd = e => Post(() => CompleteToolStep(e.Tool, e.Status, e.Result)),
            OnConfirmationRequired = e => Post(() => HandleConfirmationRequired(e)),
            OnRetrievalContext = e => Post(() => LatestRetrieval = e),
            OnDone = e => Post(() => HandleDone(e)),
            OnError = e => Post(() => HandleError(e.Error)),
        };

        try
        {
            await _streamClient.StreamChatAsync(new StreamChatOptions
            {
                Message = message,
                SessionId = ActiveSessionId,
                ProjectId = ProjectId,
                ChatMode = ChatMode,
                ApprovedActionIds = approvedActionIds.Count > 0 ? approvedActionIds : null,
                Attachments = attachments,
                Callbacks = callbacks,
            }, ct).ConfigureAwait(false);
        }
        catch (ApiException ex) when (ex.StatusCode == 429)
        {
            Post(() => HandleError("Jarvis is under heavy load right now (governor limit). Try again shortly."));
        }
        catch (Exception ex) when (ex is not OperationCanceledException)
        {
            Post(() => HandleError(ex.Message));
        }
        finally
        {
            Post(() => IsLoading = false);
        }
    }

    private void AppendToken(string delta)
    {
        if (_streamingMessage is null) return;
        _streamingMessage.Content += delta;
    }

    private void AppendReasoning(string delta)
    {
        if (_streamingMessage is null) return;
        _streamingMessage.ReasoningContent += delta;
        IsReasoningStreaming = true;
    }

    private void AddToolStep(string tool, Dictionary<string, object?> args, ToolStatus status)
    {
        _streamingMessage?.ToolSteps.Add(new ToolStep { Tool = tool, Args = args, Status = status });
        ActivitySteps.Add(new ActivityStep { Type = "tool_call", Tool = tool, Status = "running", Args = args });
    }

    private void CompleteToolStep(string tool, string status, object? result)
    {
        var step = _streamingMessage?.ToolSteps.LastOrDefault(s => s.Tool == tool && s.Status == ToolStatus.Running);
        if (step is not null)
        {
            step.Status = status == "success" ? ToolStatus.Success : ToolStatus.Error;
            step.Result = result;
        }

        var resultText = result switch
        {
            null => null,
            string s => s,
            _ => System.Text.Json.JsonSerializer.Serialize(result),
        };
        ActivitySteps.Add(new ActivityStep { Type = "tool_result", Tool = tool, Status = status, Result = resultText });
    }

    private void HandleConfirmationRequired(SseConfirmationRequiredEvent e)
    {
        foreach (var confirmation in e.PendingConfirmations)
        {
            PendingConfirmations.Add(confirmation);
        }
        if (_streamingMessage is not null)
        {
            _streamingMessage.PendingConfirmations = e.PendingConfirmations;
            _streamingMessage.Spoken = e.Spoken;
            if (!string.IsNullOrEmpty(e.Response))
            {
                _streamingMessage.Content = e.Response;
            }
        }
    }

    private void HandleDone(SseDoneEvent e)
    {
        if (_streamingMessage is null) return;
        _streamingMessage.IsStreaming = false;
        _streamingMessage.Model = e.Model;
        _streamingMessage.Provider = e.Provider;
        _streamingMessage.RouteReason = e.RouteReason;
        _streamingMessage.ActiveSkills = e.ActiveSkills;
        _streamingMessage.ToolsUsed = e.ToolsUsed;
        if (!string.IsNullOrEmpty(e.Response) && string.IsNullOrEmpty(_streamingMessage.Content))
        {
            _streamingMessage.Content = e.Response;
        }
        // Defensive: the client's own accumulation should already match, but fall back to the
        // backend's final tally if a delta was ever dropped (mirrors the Content fallback above).
        if (!string.IsNullOrEmpty(e.Reasoning) && string.IsNullOrEmpty(_streamingMessage.ReasoningContent))
        {
            _streamingMessage.ReasoningContent = e.Reasoning;
        }
        var finished = _streamingMessage;
        _streamingMessage = null;
        ActiveReasoningMessage = finished;
        IsReasoningStreaming = false;
        MessageCompleted?.Invoke(finished);
    }

    private void HandleError(string message)
    {
        Error = message;
        if (_streamingMessage is not null)
        {
            _streamingMessage.IsStreaming = false;
        }
        IsReasoningStreaming = false;
    }

    private void Post(Action action)
    {
        if (_dispatcher.HasThreadAccess)
        {
            action();
        }
        else
        {
            _dispatcher.TryEnqueue(() => action());
        }
    }
}
