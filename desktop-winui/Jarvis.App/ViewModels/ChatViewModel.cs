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

    /// <param name="voiceRoute">Voice mode's route for a spoken turn ("chat" | "act"); null when typed.</param>
    public async Task SendMessageAsync(string content, List<Attachment>? attachments = null, string? voiceRoute = null, List<string>? imagePaths = null)
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
        Messages.Add(new ChatMessage { Role = MessageRole.User, Content = displayContent, ImagePaths = imagePaths ?? new List<string>() });

        var attachmentPayload = attachments?.Select(a => new Dictionary<string, object?>
        {
            ["id"] = a.Id,
            ["filename"] = a.Filename,
            ["path"] = a.Path,
        }).ToList();

        await RunStreamAsync(content, new List<string>(), attachmentPayload, voiceRoute).ConfigureAwait(false);
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
        if (Space == ChatSpace.Freeform) return LoadThreadAsync(); // Freeform is the one thread (PLAN 4.18)
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
        LatestRetrieval = null;
        Error = null;
    }

    /// <summary>Switches to an existing session and loads its history. The backend returns raw
    /// message dicts (its persisted schema, not the SSE contract), so fields are read
    /// defensively — anything missing just renders as an empty string rather than throwing.
    /// Opening a stored session leaves ephemeral mode (the scratchpad is purged).</summary>
    public async Task LoadSessionAsync(string sessionId)
    {
        if (sessionId == ThreadId)
        {
            await LoadThreadAsync().ConfigureAwait(false);
            return;
        }
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
                foreach (var msg in MapHistory(raw)) Messages.Add(msg);
            });
        }
        catch (Exception ex)
        {
            Post(() => Error = ex.Message);
        }
    }

    /// <summary>Stored rows as transcript items. Stored tool rows come before the answer they led
    /// to: they go back on it as its tool steps, so a reopened chat shows its tool rows,
    /// "Remembered: ..." lines and screenshots as they were. A tool row's thinking (the round that
    /// made the call) goes before its step, the answer's after the last one (PLAN 4.23).</summary>
    private static List<ChatMessage> MapHistory(IEnumerable<Dictionary<string, object?>> raw)
    {
        var items = new List<ChatMessage>();
        var steps = new List<ToolStep>();
        var thinking = new List<ThinkingSegment>();
        foreach (var entry in raw)
        {
            var msg = MapRawMessage(entry);
            if (msg.Role == MessageRole.System && ReadString(entry, "name") == "compaction")
            {
                items.Add(CompactedMark(msg.Content, msg.CreatedAt));
                continue;
            }
            if (msg.Role == MessageRole.Tool)
            {
                if (!string.IsNullOrWhiteSpace(msg.ReasoningContent))
                {
                    thinking.Add(new ThinkingSegment { BeforeStep = steps.Count, Text = msg.ReasoningContent });
                }
                steps.Add(MapToolStep(entry));
                continue;
            }
            if (steps.Count > 0)
            {
                var owner = msg;
                if (msg.Role != MessageRole.Assistant)
                {
                    // tools that ran in a turn which ended without an answer (a confirmation)
                    owner = new ChatMessage { Role = MessageRole.Assistant, HasStructuredReasoning = true };
                    items.Add(owner);
                }
                owner.ToolSteps.AddRange(steps);
                owner.Thinking.InsertRange(0, thinking);
                steps.Clear();
                thinking.Clear();
            }
            if (msg.Role == MessageRole.Assistant && msg.HasStructuredReasoning)
            {
                msg.Thinking.Add(new ThinkingSegment { BeforeStep = msg.ToolSteps.Count, Text = msg.ReasoningContent });
            }
            items.Add(msg);
        }
        if (steps.Count > 0)
        {
            items.Add(new ChatMessage { Role = MessageRole.Assistant, HasStructuredReasoning = true });
            items[^1].ToolSteps.AddRange(steps);
            items[^1].Thinking.AddRange(thinking);
        }
        return items;
    }

    /// <summary>The "Conversation compacted" divider (PLAN 4.25), opening to the summary.</summary>
    internal static ChatMessage CompactedMark(string summary, DateTimeOffset at) => new()
    {
        Role = MessageRole.System,
        Mark = ThreadMark.Compacted,
        CreatedAt = at,
        MarkText = $"Conversation compacted · {at.LocalDateTime:HH:mm}",
        MarkDetail = summary,
    };

    /// <summary>A compaction this turn reported: after the last turn (it goes above the new
    /// message), or /compact (above the reply).</summary>
    private void InsertCompacted(SseCompactedEvent e, ChatMessage reply)
    {
        var mark = CompactedMark(e.Summary, e.At > 0 ? FromUnix(e.At) : DateTimeOffset.Now);
        var at = Messages.IndexOf(reply);
        if (at < 0) return;
        if (!e.Manual && at > 0 && Messages[at - 1].Role == MessageRole.User) at--;
        Messages.Insert(at, mark);
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
            // Thread pages carry the stored id and time (day dividers, scroll-back); /sessions rows don't.
            StoredId = ReadNumber(entry, "id") is double id ? (long)id : null,
            CreatedAt = ReadNumber(entry, "created_at") is double at ? FromUnix(at) : DateTimeOffset.Now,
        };
    }

    /// <summary>A stored tool row (its result, and the call it carries) as a finished tool step.</summary>
    private static ToolStep MapToolStep(Dictionary<string, object?> entry)
    {
        var result = ReadString(entry, "content") ?? "";
        var step = new ToolStep
        {
            Tool = ReadString(entry, "name") ?? "tool",
            Result = result,
            Status = result.StartsWith("Error", StringComparison.Ordinal) ? ToolStatus.Error : ToolStatus.Success,
        };
        try
        {
            if (entry.TryGetValue("tool_calls", out var calls) && calls is System.Text.Json.JsonElement { ValueKind: System.Text.Json.JsonValueKind.Array } list
                && list.GetArrayLength() > 0
                && list[0].TryGetProperty("function", out var function)
                && function.TryGetProperty("arguments", out var args))
            {
                var json = args.ValueKind == System.Text.Json.JsonValueKind.String ? args.GetString() ?? "{}" : args.GetRawText();
                step.Args = System.Text.Json.JsonSerializer.Deserialize<Dictionary<string, object?>>(json) ?? new();
            }
        }
        catch (System.Text.Json.JsonException)
        {
            // malformed stored arguments: the row shows without them
        }
        return step;
    }

    /// <summary>The Undo on a "Remembered: ..." / "Forgot: ..." line. Returns what the line says
    /// afterwards, or null (and the error bar says why) when nothing changed.</summary>
    public async Task<string?> UndoMemoryAsync(ToolStep step, Views.MemoryLine line)
    {
        if (line.ShortId is null) return null;
        try
        {
            var ok = line.IsForget
                ? await _api.RestoreForgottenMemoryAsync(line.ShortId)
                : await _api.UndoRememberAsync(line.ShortId);
            if (ok) return line.IsForget ? $"Restored: {line.Content}" : $"Removed (kept in the bin): {line.Content}";
            Error = line.IsForget
                ? "That memory isn't in the bin any more (already restored, or older than 30 days)."
                : "That memory is already gone.";
        }
        catch (Exception ex)
        {
            Error = ex.Message;
        }
        return null;
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

    private async Task RunStreamAsync(string message, List<string> approvedActionIds, List<Dictionary<string, object?>>? attachments = null, string? voiceRoute = null)
    {
        _abort?.Cancel();
        _abort = new CancellationTokenSource();
        var ct = _abort.Token;

        IsLoading = true;
        Error = null;
        // The user just acted in this window, so it may pass on the right to raise windows.
        Services.ForegroundGrant.ToBackend();

        var newTopic = TakeNewTopic();
        _streamingMessage = new ChatMessage { Role = MessageRole.Assistant, IsStreaming = true, HasStructuredReasoning = true };
        Messages.Add(_streamingMessage);

        var callbacks = new ChatStreamCallbacks
        {
            OnToken = e => Post(() => AppendToken(e.Delta)),
            OnReasoning = e => Post(() => _streamingMessage?.AppendThinking(e.Delta)),
            OnThinkingStatus = e => Post(() => _streamingMessage?.SetThinkingStatus(e.Text)),
            OnCompacted = e => Post(() => { if (_streamingMessage is { } reply) InsertCompacted(e, reply); }),
            OnToolStart = e => Post(() => AddToolStep(e.Tool, e.Args, ToolStatus.Running)),
            OnToolEnd = e => Post(() => CompleteToolStep(e.Tool, e.Status, e.Result)),
            OnConfirmationRequired = e => Post(() => HandleConfirmationRequired(e)),
            OnRetrievalContext = e => Post(() => LatestRetrieval = e),
            OnDone = e => Post(() => HandleDone(e)),
            OnError = e => Post(() => HandleError(e.Error)),
            OnEpisode = e => Post(() => HandleEpisode(e)),
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
                VoiceRoute = voiceRoute,
                NewTopic = newTopic,
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
        _streamingMessage.EndThinking();
        _streamingMessage.Content += delta;
    }

    private void AddToolStep(string tool, Dictionary<string, object?> args, ToolStatus status) =>
        _streamingMessage?.StartToolStep(new ToolStep { Tool = tool, Args = args, Status = status });

    private void CompleteToolStep(string tool, string status, object? result) =>
        _streamingMessage?.EndToolStep(tool, status, result);

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
            _streamingMessage.EndThinking();
            _streamingMessage.IsStreaming = false;
            _streamingMessage.Model = e.Model;
            _streamingMessage.Provider = e.Provider;
            var finished = _streamingMessage;
            _streamingMessage = null;
            MessageCompleted?.Invoke(finished);
        }
    }

    private void HandleDone(SseDoneEvent e)
    {
        // A turn that forgot earlier topics: the transcript still shows them, so reload it (the
        // reply is stored before "done" is sent). Otherwise only the episode titles may have moved.
        var forgot = IsThread && _streamingMessage?.ToolSteps.Any(s => s.Tool == "forget_conversation"
            && s.Status == ToolStatus.Success && (s.Result?.ToString() ?? "").StartsWith("Forgotten", StringComparison.Ordinal)) == true;
        if (forgot) _ = LoadThreadAsync();
        else if (IsThread) _ = RefreshEpisodesAfterTurnAsync();
        if (_streamingMessage is null) return;
        _streamingMessage.EndThinking();
        _streamingMessage.IsStreaming = false;
        _streamingMessage.Model = e.Model;
        _streamingMessage.Provider = e.Provider;
        _streamingMessage.RouteReason = e.RouteReason;
        _streamingMessage.ActiveSkills = e.ActiveSkills;
        _streamingMessage.ToolsUsed = e.ToolsUsed;
        _streamingMessage.Metrics = e.Metrics;
        _streamingMessage.CompareId = e.CompareId;
        if (!string.IsNullOrEmpty(e.CompareId))
        {
            _ = PollCompareAsync(_streamingMessage);
        }
        if (!string.IsNullOrEmpty(e.Response) && (e.ReplaceContent || string.IsNullOrEmpty(_streamingMessage.Content)))
        {
            _streamingMessage.Content = e.Response;
        }
        var finished = _streamingMessage;
        _streamingMessage = null;
        MessageCompleted?.Invoke(finished);
    }

    /// <summary>Asks for the reply's second take (PLAN 4.13 #8) every 2 s for up to 2 minutes; most
    /// end as "none" (the model judged the reply already the right shape) and nothing is shown.</summary>
    private async Task PollCompareAsync(ChatMessage message)
    {
        var id = message.CompareId!;
        for (var i = 0; i < 60; i++)
        {
            await Task.Delay(TimeSpan.FromSeconds(2)).ConfigureAwait(false);
            CompareState? state;
            try
            {
                state = await _api.FetchCompareAsync(id).ConfigureAwait(false);
            }
            catch
            {
                return;  // backend gone or restarting: the compare is an extra, never an error
            }
            if (state is null || state.Status is "none" or "failed" or "voted") return;
            if (state.Status == "ready" && !string.IsNullOrEmpty(state.Alternative))
            {
                Post(() =>
                {
                    message.CompareAxis = state.Axis;
                    message.CompareAlternative = state.Alternative;
                });
                return;
            }
        }
    }

    /// <summary>The user's pick on a compare card; the card goes away at once, the vote is saved
    /// to the preference file by the backend (a failure there is not worth an error).</summary>
    public async Task VoteCompareAsync(ChatMessage message, string choice)
    {
        if (string.IsNullOrEmpty(message.CompareId) || message.CompareVoted) return;
        message.CompareVoted = true;
        try
        {
            await _api.VoteCompareAsync(message.CompareId, choice).ConfigureAwait(false);
        }
        catch
        {
        }
    }

    private void HandleError(string message)
    {
        Error = message;
        if (_streamingMessage is not null)
        {
            _streamingMessage.EndThinking();
            _streamingMessage.IsStreaming = false;
        }
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
