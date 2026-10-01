using System.Collections.ObjectModel;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using Jarvis.Core.Api;
using Jarvis.Core.Models;
using Jarvis.Core.Sse;
using Jarvis.Core.Voice;
using Microsoft.UI.Dispatching;

namespace Jarvis_App.ViewModels;

/// <summary>
/// Port of the retired Next.js client's src/hooks/useChat.ts. Owns the transcript for one chat session and drives
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

    /// <summary>The active workspace project. Remembered while in Freeform, but only sent with a
    /// turn in the Workspace space (see <see cref="TurnProjectId"/>).</summary>
    [ObservableProperty]
    public partial string? ProjectId { get; set; }

    /// <summary>Freeform (no project, no workspace, no RAG) or Workspace (project-bound). Change it
    /// through <see cref="SwitchSpaceAsync"/>, which swaps the transcript as well.</summary>
    [ObservableProperty]
    public partial ChatSpace Space { get; private set; } = ChatSpace.Workspace;

    /// <summary>Scratchpad mode: turns go to the backend flagged ephemeral, so nothing is stored
    /// and nothing reaches the session list. Change it through <see cref="SetEphemeralAsync"/>.</summary>
    [ObservableProperty]
    public partial bool IsEphemeral { get; private set; }

    /// <summary>The user's model choice for every turn: null = Auto (the backend routes, and a
    /// quick turn runs on whatever is loaded), "main" or "fast" = that slot, loading it if it
    /// isn't -- the backend's explicit <c>model</c> skips its routing overrides (except images,
    /// which still go to a slot that can see them).</summary>
    public string? ModelChoice { get; set; }

    /// <summary>What the backend calls the space.</summary>
    public string ChatMode => Space == ChatSpace.Freeform ? "FREEFORM" : "WORKSPACE";

    /// <summary>The project a turn (or an upload) belongs to: none in Freeform.</summary>
    public string? TurnProjectId => Space == ChatSpace.Workspace ? ProjectId : null;

    /// <summary>Each space's last stored conversation, so switching back returns to it rather
    /// than to whatever the other space was showing. Ephemeral sessions are never remembered.</summary>
    private readonly Dictionary<ChatSpace, string> _lastSessionBySpace = new();

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

    /// <summary>Short spoken acknowledgements for things that resolve without a model turn
    /// (a cancelled confirmation), so a voice exchange is not left hanging in silence.</summary>
    public event Action<string>? SpokenFeedback;

    public ChatViewModel(JarvisApiClient api, ChatStreamClient streamClient, DispatcherQueue dispatcher)
    {
        _api = api;
        _streamClient = streamClient;
        _dispatcher = dispatcher;
    }

    public async Task SendMessageAsync(string content, List<Attachment>? attachments = null)
    {
        var hasAttachments = attachments is { Count: > 0 };
        if (string.IsNullOrWhiteSpace(content) && !hasAttachments)
        {
            return;
        }

        // While an action is waiting, a reply is first read as the answer to that question --
        // the backend's spoken prompt literally says "say yes to proceed, or no to cancel". Only
        // a clear yes approves; anything that is not an answer goes to the model as an ordinary
        // message with nothing approved, and the card stays until it is answered or the
        // backend's confirmation window lapses. (Every message used to carry every pending
        // action id, so "no" and unrelated questions ran the action.)
        if (PendingConfirmations.Count > 0 && !hasAttachments)
        {
            switch (ConfirmationIntentParser.Parse(content))
            {
                case ConfirmationIntent.Yes:
                    await ApproveAllPendingAsync(content).ConfigureAwait(false);
                    return;
                case ConfirmationIntent.No:
                    DenyAllPending(content);
                    return;
            }
        }

        var displayContent = content;
        if (string.IsNullOrWhiteSpace(displayContent) && hasAttachments)
        {
            displayContent = $"Uploaded {attachments!.Count} file(s): {string.Join(", ", attachments.Select(a => a.Filename))}";
        }
        _lastUserPrompt = displayContent;
        Messages.Add(new ChatMessage { Role = MessageRole.User, Content = displayContent });

        var attachmentPayload = attachments?.Select(a => new Dictionary<string, object?>
        {
            ["id"] = a.Id,
            ["filename"] = a.Filename,
            ["path"] = a.Path,
        }).ToList();

        await RunStreamAsync(content, new List<string>(), attachmentPayload).ConfigureAwait(false);
    }

    [RelayCommand]
    public Task ConfirmActionAsync(PendingConfirmation confirmation)
    {
        // The card may be clicked after a typed/spoken "yes" already resolved it; the id must
        // not be sent twice, since the backend's hash check would run the action again.
        if (!PendingConfirmations.Contains(confirmation)) return Task.CompletedTask;
        Resolve(new[] { confirmation });
        return RunStreamAsync(_lastUserPrompt, new List<string> { confirmation.ActionId });
    }

    [RelayCommand]
    public void DenyAction(PendingConfirmation confirmation)
    {
        if (!PendingConfirmations.Contains(confirmation)) return;
        Resolve(new[] { confirmation });
        Messages.Add(new ChatMessage
        {
            Role = MessageRole.System,
            Content = "Action execution was denied by user.",
        });
        NotifyDenied(new[] { confirmation });
    }

    /// <summary>A typed or spoken "yes": every waiting action is approved in one resubmission,
    /// the same way the approve button does it, and the user's own words stay in the transcript
    /// so the turn reads as a conversation rather than a card that vanished.
    ///
    /// An approval resubmits the prompt that led to the ask, with the approved ids attached:
    /// the backend re-plans the same tool call, whose id now matches, and runs it. The message
    /// itself cannot be empty -- /chat/stream rejects that with a 422.</summary>
    private Task ApproveAllPendingAsync(string utterance)
    {
        var pending = PendingConfirmations.ToList();
        Resolve(pending);
        Messages.Add(new ChatMessage { Role = MessageRole.User, Content = utterance.Trim() });
        return RunStreamAsync(_lastUserPrompt, pending.Select(p => p.ActionId).ToList());
    }

    private void DenyAllPending(string utterance)
    {
        var pending = PendingConfirmations.ToList();
        Resolve(pending);
        Messages.Add(new ChatMessage { Role = MessageRole.User, Content = utterance.Trim() });
        Messages.Add(new ChatMessage
        {
            Role = MessageRole.System,
            Content = pending.Count == 1
                ? "Action execution was denied by user."
                : $"{pending.Count} actions were denied by user.",
        });
        NotifyDenied(pending);
        // Nothing is sent to the model, so there is no reply to speak; the voice loop still
        // needs to hear that it was understood.
        SpokenFeedback?.Invoke("Understood, cancelled.");
    }

    /// <summary>Takes the confirmations out of the live list and off the bubble that shows
    /// their card, so the card collapses instead of offering an approve button for an action
    /// that has already been decided.</summary>
    private void Resolve(IEnumerable<PendingConfirmation> confirmations)
    {
        foreach (var confirmation in confirmations)
        {
            PendingConfirmations.Remove(confirmation);
            foreach (var message in Messages)
            {
                if (message.PendingConfirmations.Contains(confirmation))
                {
                    message.ResolveConfirmation(confirmation);
                }
            }
        }
    }

    /// <summary>Tells the backend, otherwise its confirmation window keeps running and Jarvis
    /// later announces a timeout for something the user already said no to. Fire-and-forget:
    /// the card is gone either way and the backend's timeout is the fallback.</summary>
    private void NotifyDenied(IEnumerable<PendingConfirmation> confirmations)
    {
        var ids = confirmations.Select(c => c.ActionId).ToList();
        _ = Task.Run(async () =>
        {
            foreach (var id in ids)
            {
                try
                {
                    await _api.DenyConfirmationAsync(id).ConfigureAwait(false);
                }
                catch
                {
                    // best-effort
                }
            }
        });
    }

    [RelayCommand]
    public void AbortStream()
    {
        _abort?.Cancel();
    }

    /// <summary>A fresh conversation in the current space. In ephemeral mode this is the reset:
    /// the old scratchpad is purged on the backend and a new one begins.</summary>
    public void NewChat()
    {
        if (IsEphemeral)
        {
            PurgeEphemeral(ActiveSessionId);
            ClearTranscript();
            ActiveSessionId = JarvisApiClient.CreateEphemeralSessionId();
            return;
        }
        ClearTranscript();
        ActiveSessionId = JarvisApiClient.CreateNewSessionId();
    }

    /// <summary>Moves to the other space. Nothing crosses over: the transcript is swapped for
    /// that space's last conversation (or a new one), and an ephemeral scratchpad is purged and
    /// restarted empty in the new space rather than carried into it.</summary>
    public async Task SwitchSpaceAsync(ChatSpace target)
    {
        if (target == Space) return;
        Space = target;
        OnPropertyChanged(nameof(ChatMode));
        OnPropertyChanged(nameof(TurnProjectId));
        if (IsEphemeral)
        {
            NewChat();
            return;
        }
        await RestoreSpaceSessionAsync().ConfigureAwait(false);
    }

    /// <summary>Turns scratchpad mode on or off. On: the stored conversation is set aside (and
    /// returned to later) and an empty ephemeral one starts. Off: the ephemeral conversation is
    /// purged on the backend at once and the space's stored conversation comes back.</summary>
    public async Task SetEphemeralAsync(bool on)
    {
        if (on == IsEphemeral) return;
        if (on)
        {
            IsEphemeral = true;
            ClearTranscript();
            ActiveSessionId = JarvisApiClient.CreateEphemeralSessionId();
            return;
        }
        PurgeEphemeral(ActiveSessionId);
        IsEphemeral = false;
        await RestoreSpaceSessionAsync().ConfigureAwait(false);
    }

    private Task RestoreSpaceSessionAsync()
    {
        if (_lastSessionBySpace.TryGetValue(Space, out var last)) return LoadSessionAsync(last);
        NewChat();
        return Task.CompletedTask;
    }

    partial void OnActiveSessionIdChanged(string value)
    {
        if (!IsEphemeral) _lastSessionBySpace[Space] = value;
    }

    /// <summary>Drops the ephemeral conversation's in-memory context and uploads on the backend.
    /// Fire-and-forget: the transcript is already gone here, and the backend's idle expiry is the
    /// fallback if the call fails.</summary>
    private void PurgeEphemeral(string sessionId)
    {
        _ = Task.Run(async () =>
        {
            try
            {
                await _api.DeleteSessionAsync(sessionId).ConfigureAwait(false);
            }
            catch (Exception ex)
            {
                App.Log($"ephemeral purge failed: {ex.GetType().Name}: {ex.Message}");
            }
        });
    }

    private void ClearTranscript()
    {
        _abort?.Cancel();
        Messages.Clear();
        PendingConfirmations.Clear();
        ActivitySteps.Clear();
        LatestRetrieval = null;
        ActiveReasoningMessage = null;
        IsReasoningStreaming = false;
        Error = null;
    }

    /// <summary>Switches to an existing session and loads its history. The backend returns raw
    /// message dicts (its persisted schema, not the SSE contract), so fields are read
    /// defensively — anything missing just renders as an empty string rather than throwing.
    /// Opening a stored session leaves ephemeral mode (the scratchpad is purged).</summary>
    public async Task LoadSessionAsync(string sessionId)
    {
        if (IsEphemeral)
        {
            PurgeEphemeral(ActiveSessionId);
            IsEphemeral = false;
        }
        ClearTranscript();
        ActiveSessionId = sessionId;

        try
        {
            var raw = await _api.FetchSessionMessagesAsync(sessionId).ConfigureAwait(false);
            // ConfigureAwait(false) leaves us on a worker thread; Messages is bound to the
            // ListView, and WinUI refuses ObservableCollection changes off the UI thread. Doing
            // the adds inline here threw a COMException that only surfaced as an empty chat.
            Post(() =>
            {
                if (ActiveSessionId != sessionId) return; // user already switched again
                foreach (var entry in raw)
                {
                    var msg = MapRawMessage(entry);
                    // Stored tool results are the model's memory of what a tool returned, not
                    // part of the transcript the user saw.
                    if (msg.Role == MessageRole.Tool) continue;
                    Messages.Add(msg);
                    if (msg.Role == MessageRole.Assistant)
                    {
                        ActiveReasoningMessage = msg;
                    }
                }
            });
        }
        catch (Exception ex)
        {
            Post(() => Error = ex.Message);
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
                ProjectId = TurnProjectId,
                Model = ModelChoice,
                ChatMode = ChatMode,
                Ephemeral = IsEphemeral,
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
            // The backend ends the stream here without a "done" event, so this is the turn's
            // completion: the caret has to go, and the spoken "say yes to proceed, or no to
            // cancel" prompt is only spoken through MessageCompleted.
            _streamingMessage.IsStreaming = false;
            _streamingMessage.Model = e.Model;
            _streamingMessage.Provider = e.Provider;
            var finished = _streamingMessage;
            _streamingMessage = null;
            IsReasoningStreaming = false;
            MessageCompleted?.Invoke(finished);
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

/// <summary>The two places a conversation can live. Freeform: direct conversation, no project,
/// no working directory, no retrieval. Workspace: bound to the active project -- its files,
/// artifacts, repository tools and RAG.</summary>
public enum ChatSpace
{
    Freeform,
    Workspace,
}
