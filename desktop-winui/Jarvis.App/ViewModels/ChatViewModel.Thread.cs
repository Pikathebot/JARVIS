using System.Text.Json;
using CommunityToolkit.Mvvm.ComponentModel;
using Jarvis.Core.Api;
using Jarvis.Core.Models;
using Jarvis.Core.Sse;

namespace Jarvis_App.ViewModels;

/// <summary>
/// The one continuing Freeform thread (PLAN 4.18). Freeform's main conversation is the fixed
/// session <c>jarvis-thread</c>: one scroll on screen, cut into episodes by the backend (a 3-hour
/// gap, or New topic). The transcript gets marks between the messages -- a day divider, an
/// episode marker, the episode's digest card (collapsed) -- and loads a page at a time, older
/// pages arriving as the view nears the top. "New chat" in Freeform still makes a side chat.
/// </summary>
public partial class ChatViewModel
{
    public const string ThreadId = JarvisApiClient.ThreadSessionId;
    private const int ThreadPageSize = 80;

    public bool IsThread => ActiveSessionId == ThreadId && !IsEphemeral;

    /// <summary>The thread's episodes, newest first (the sidebar's table of contents).</summary>
    public IReadOnlyList<ThreadEpisode> Episodes { get; private set; } = Array.Empty<ThreadEpisode>();

    public event Action? EpisodesChanged;

    /// <summary>Asks the window to bring the transcript item at this index into view.</summary>
    public event Action<int>? ScrollToItemRequested;

    /// <summary>The New topic button is armed: the next message opens a new episode.</summary>
    [ObservableProperty]
    public partial bool NewTopicPending { get; set; }

    private long? _threadOldestId;
    private bool _threadHasMore;
    private bool _loadingOlder;
    private ChatMessage? _pendingTopicMark;
    private bool _sentWithTopicMark;

    // ---- other devices' turns, live (PLAN 4.19) ----------------------------------------------

    private readonly Dictionary<string, ChatMessage> _liveTurns = new(); // UI thread only
    private CancellationTokenSource? _liveCts;

    /// <summary>Keeps a connection to the backend's live thread relay (reconnecting every few
    /// seconds while it is down), so a turn sent from the phone streams here as it happens.</summary>
    public void StartThreadLive()
    {
        if (_liveCts is not null) return;
        _liveCts = new CancellationTokenSource();
        var ct = _liveCts.Token;
        _ = Task.Run(async () =>
        {
            while (!ct.IsCancellationRequested)
            {
                try
                {
                    await _streamClient.ConnectThreadLiveAsync(OnLiveTurnStart, OnLiveTurnEnd, ct).ConfigureAwait(false);
                }
                catch (Exception ex) when (ex is not OperationCanceledException)
                {
                    // backend starting or restarting: try again shortly
                }
                catch (OperationCanceledException)
                {
                    return;
                }
                await Task.Delay(TimeSpan.FromSeconds(3), ct).ConfigureAwait(false);
            }
        }, ct);
    }

    public void StopThreadLive()
    {
        _liveCts?.Cancel();
        _liveCts = null;
    }

    /// <summary>A turn another device started: its message (unless it resumes an approval, whose
    /// prompt is already shown) and a streaming answer, drawn only while the thread is open here.
    /// Runs on the stream's thread: every change is posted to the UI thread, in order.</summary>
    private ChatStreamCallbacks OnLiveTurnStart(string turnId, string message, bool resumed)
    {
        var answer = new ChatMessage { Role = MessageRole.Assistant, IsStreaming = true, HasStructuredReasoning = true };
        Post(() =>
        {
            if (!IsThread) return;
            if (!resumed) Messages.Add(new ChatMessage { Role = MessageRole.User, Content = message, CreatedAt = DateTimeOffset.Now });
            Messages.Add(answer);
            _liveTurns[turnId] = answer;
        });
        return new ChatStreamCallbacks
        {
            OnToken = e => Post(() => { answer.EndThinking(); answer.Content += e.Delta; }),
            OnReasoning = e => Post(() => answer.AppendThinking(e.Delta)),
            OnThinkingStatus = e => Post(() => answer.SetThinkingStatus(e.Text)),
            OnCompacted = e => Post(() => { if (IsThread) InsertCompacted(e, answer); }),
            OnToolStart = e => Post(() => answer.StartToolStep(new ToolStep { Tool = e.Tool, Args = e.Args, Status = ToolStatus.Running })),
            OnToolEnd = e => Post(() => answer.EndToolStep(e.Tool, e.Status, e.Result)),
            // The approval belongs to the device that asked: shown here as words, not buttons,
            // so the same action can't be approved twice.
            OnConfirmationRequired = e => Post(() =>
            {
                answer.Content = (string.IsNullOrEmpty(e.Response) ? answer.Content : e.Response)
                    + "\n\nWaiting for your answer on the device that asked.";
                answer.IsStreaming = false;
            }),
            OnDone = e => Post(() =>
            {
                if (!string.IsNullOrEmpty(e.Response) && (e.ReplaceContent || string.IsNullOrEmpty(answer.Content))) answer.Content = e.Response;
                answer.Model = e.Model;
                answer.Provider = e.Provider;
                answer.EndThinking();
                answer.IsStreaming = false;
            }),
            OnError = e => Post(() => { answer.Content += $"\n\nSomething went wrong: {e.Error}"; answer.IsStreaming = false; }),
        };
    }

    private void OnLiveTurnEnd(string turnId) => Post(() =>
    {
        if (_liveTurns.Remove(turnId, out var answer)) answer.IsStreaming = false;
        _sawOwnTurn = true; // what was relayed is what was stored: the next head check adopts it
        if (IsThread) _ = RefreshEpisodesAfterTurnAsync();
    });

    private long? _threadHead;     // the newest stored message this window has shown or adopted
    private bool _sawOwnTurn;      // a turn of ours ran since the last head check

    partial void OnIsLoadingChanged(bool value)
    {
        if (value) _sawOwnTurn = true;
    }

    /// <summary>Whether another device added turns to the thread (the phone, PLAN 4.19): asked
    /// every few seconds by the window, which then reloads the thread on the UI thread (the
    /// transcript collection may only change there; LoadThreadAsync clears it before its first
    /// await). True when the newest message isn't one this window has seen -- never while a turn
    /// streams or an approval waits here. After a turn of our own the new head is only adopted
    /// (it is already on screen).</summary>
    public async Task<bool> ThreadChangedElsewhereAsync()
    {
        var idle = await OnUiAsync(() => IsThread && !IsLoading && PendingConfirmations.Count == 0 && _liveTurns.Count == 0).ConfigureAwait(false);
        if (!idle) return false;
        long head;
        try
        {
            head = await _api.FetchThreadHeadAsync().ConfigureAwait(false);
        }
        catch
        {
            return false; // offline: the governor poll already says so
        }
        return await OnUiAsync(() =>
        {
            if (!IsThread || IsLoading || PendingConfirmations.Count > 0 || _liveTurns.Count > 0) return false;
            var adopt = _threadHead is null || _sawOwnTurn;
            _sawOwnTurn = false;
            var changed = _threadHead != head;
            _threadHead = head;
            return changed && !adopt;
        }).ConfigureAwait(false);
    }

    public async Task LoadThreadAsync()
    {
        if (IsEphemeral)
        {
            PurgeEphemeral(ActiveSessionId);
            IsEphemeral = false;
        }
        ClearTranscript();
        _liveTurns.Clear(); // their answers just left the transcript; the reload shows what is stored
        NewTopicPending = false;
        _pendingTopicMark = null;
        ActiveSessionId = ThreadId;
        try
        {
            var episodes = _api.FetchThreadEpisodesAsync();
            var page = await _api.FetchThreadMessagesAsync(limit: ThreadPageSize).ConfigureAwait(false);
            var list = await episodes.ConfigureAwait(false);
            Post(() =>
            {
                if (ActiveSessionId != ThreadId) return;
                SetEpisodes(list);
                _threadHasMore = page.HasMore;
                _threadOldestId = OldestId(page);
                _threadHead = page.Messages.Count > 0 && ReadNumber(page.Messages[^1], "id") is double newest ? (long)newest : 0;
                _sawOwnTurn = false;
                foreach (var item in BuildThreadItems(page.Messages)) Messages.Add(item);
            });
        }
        catch (HttpRequestException ex)
        {
            // The window opens before the backend listens: the load is retried when it does
            // (MainWindow.OnBackendCameOnline), so "actively refused" is not the user's error.
            App.Log($"thread load before the backend was up: {ex.Message}");
        }
        catch (Exception ex)
        {
            Post(() => Error = ex.Message);
        }
    }

    /// <summary>The page before the oldest loaded message, inserted at the top. False when there
    /// is nothing older (or a load is already running).</summary>
    public async Task<bool> LoadOlderThreadAsync()
    {
        if (!IsThread || !_threadHasMore || _loadingOlder || _threadOldestId is null) return false;
        _loadingOlder = true;
        try
        {
            var page = await _api.FetchThreadMessagesAsync(_threadOldestId, ThreadPageSize).ConfigureAwait(false);
            var done = new TaskCompletionSource<bool>();
            Post(() =>
            {
                if (!IsThread) { done.TrySetResult(false); return; }
                var items = BuildThreadItems(page.Messages);
                // The page ends on the day the loaded part starts on: one divider, not two.
                if (Messages.Count > 0 && Messages[0].Mark == ThreadMark.Day && items.Count > 0
                    && items[^1].CreatedAt.Date == Messages[0].CreatedAt.Date)
                {
                    Messages.RemoveAt(0);
                }
                for (var i = 0; i < items.Count; i++) Messages.Insert(i, items[i]);
                _threadHasMore = page.HasMore;
                _threadOldestId = OldestId(page) ?? _threadOldestId;
                done.TrySetResult(items.Count > 0);
            });
            return await done.Task.ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            App.Log($"thread scroll-back failed: {ex.GetType().Name}: {ex.Message}");
            return false;
        }
        finally
        {
            _loadingOlder = false;
        }
    }

    /// <summary>Shows the thread (if another chat is open) and scrolls to an episode's marker,
    /// loading older pages until it is there.</summary>
    public async Task JumpToEpisodeAsync(long episodeId)
    {
        if (!IsThread) await LoadThreadAsync().ConfigureAwait(false);
        for (var guard = 0; guard < 50; guard++)
        {
            var index = await OnUiAsync(() => IndexOfEpisode(episodeId)).ConfigureAwait(false);
            if (index >= 0)
            {
                Post(() => ScrollToItemRequested?.Invoke(index));
                return;
            }
            if (!await LoadOlderThreadAsync().ConfigureAwait(false)) return;
        }
    }

    /// <summary>The New topic button: arms the next message to open a new episode, shown at once
    /// as a marker at the end of the transcript; pressing it again disarms it.</summary>
    public void ToggleNewTopic()
    {
        if (!IsThread) return;
        if (NewTopicPending)
        {
            if (_pendingTopicMark is not null) Messages.Remove(_pendingTopicMark);
            _pendingTopicMark = null;
            NewTopicPending = false;
            return;
        }
        _pendingTopicMark = new ChatMessage
        {
            Role = MessageRole.System, Mark = ThreadMark.Episode,
            MarkText = "New topic · starts with your next message",
        };
        Messages.Add(_pendingTopicMark);
        NewTopicPending = true;
    }

    /// <summary>Called as a message is sent: whether it carries new_topic. The armed marker takes
    /// its place above the user's message and gets the time.</summary>
    private bool TakeNewTopic()
    {
        _sentWithTopicMark = false;
        if (!IsThread || !NewTopicPending) return false;
        NewTopicPending = false;
        if (_pendingTopicMark is not null)
        {
            _pendingTopicMark.MarkText = $"New topic · {DateTime.Now:HH:mm}";
            _sentWithTopicMark = true;
        }
        _pendingTopicMark = null;
        return true;
    }

    /// <summary>The backend opened an episode for this turn (a 3-hour gap, or "new topic" typed):
    /// put its marker above the user's message, unless the New topic button already did.</summary>
    private void HandleEpisode(SseEpisodeEvent e)
    {
        if (!IsThread || !e.Opened) return;
        if (_sentWithTopicMark) return;
        var lastUser = -1;
        for (var i = Messages.Count - 1; i >= 0; i--)
        {
            if (Messages[i].Role == MessageRole.User) { lastUser = i; break; }
        }
        if (lastUser < 0) return;
        var mark = new ChatMessage
        {
            Role = MessageRole.System, Mark = ThreadMark.Episode, EpisodeId = e.Id,
            MarkText = $"New topic · {DateTime.Now:HH:mm}",
        };
        Messages.Insert(lastUser, mark);
        if (!HasDayMarkFor(lastUser, DateTime.Now.Date))
        {
            Messages.Insert(lastUser, DayMark(DateTimeOffset.Now));
        }
    }

    private bool HasDayMarkFor(int index, DateTime day)
    {
        for (var i = index - 1; i >= 0; i--)
        {
            var m = Messages[i];
            if (m.Mark == ThreadMark.Day) return m.CreatedAt.Date == day;
            if (m.Mark == ThreadMark.None && m.CreatedAt.Date != day) return false;
        }
        return false;
    }

    /// <summary>The sidebar's "Delete topic": gone for good on the backend, then the thread (if it
    /// is open) and the table of contents are reloaded without it.</summary>
    public async Task<bool> DeleteEpisodeAsync(long episodeId)
    {
        bool ok;
        try
        {
            ok = await _api.DeleteThreadEpisodeAsync(episodeId).ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            Post(() => Error = ex.Message);
            return false;
        }
        if (!ok)
        {
            Post(() => Error = "That topic couldn't be deleted.");
            return false;
        }
        await AfterThreadForgotAsync().ConfigureAwait(false);
        return true;
    }

    /// <summary>Topics were deleted (the sidebar, or the forget_conversation tool): what is on
    /// screen may still show them, so reload.</summary>
    private async Task AfterThreadForgotAsync()
    {
        if (await OnUiAsync(() => IsThread && !IsLoading).ConfigureAwait(false)) await LoadThreadAsync().ConfigureAwait(false);
        else await RefreshEpisodesAsync().ConfigureAwait(false);
    }

    /// <summary>After a reply: the episode list now, and again once the backend has had time to
    /// name the topic (it asks the model in the background, ~1 s, more while a turn queues).</summary>
    private async Task RefreshEpisodesAfterTurnAsync()
    {
        await RefreshEpisodesAsync().ConfigureAwait(false);
        foreach (var wait in new[] { 2500, 6000 })
        {
            await Task.Delay(wait).ConfigureAwait(false);
            if (!await OnUiAsync(() => IsThread).ConfigureAwait(false)) return;
            await RefreshEpisodesAsync().ConfigureAwait(false);
        }
    }

    private async Task RefreshEpisodesAsync()
    {
        try
        {
            var list = await _api.FetchThreadEpisodesAsync().ConfigureAwait(false);
            Post(() => SetEpisodes(list));
        }
        catch (Exception ex)
        {
            App.Log($"thread episodes refresh failed: {ex.GetType().Name}: {ex.Message}");
        }
    }

    /// <summary>New episode data: the sidebar list, and markers/digest cards already on screen
    /// get their titles (a record written since) and an id (a marker placed live).</summary>
    private void SetEpisodes(IReadOnlyList<ThreadEpisode> list)
    {
        Episodes = list;
        var open = list.FirstOrDefault(e => e.RecordStatus == "open");
        for (var i = 0; i < Messages.Count; i++)
        {
            var m = Messages[i];
            if (m.Mark != ThreadMark.Episode) continue;
            if (m.EpisodeId is null && open is not null && i >= Messages.Count - 3) m.EpisodeId = open.Id;
            var ep = list.FirstOrDefault(e => e.Id == m.EpisodeId);
            if (ep is null) continue;
            var text = EpisodeLine(ep);
            m.MarkText = text;
            var hasDigest = i + 1 < Messages.Count && Messages[i + 1].Mark == ThreadMark.Digest;
            if (!hasDigest && !string.IsNullOrWhiteSpace(ep.Digest))
            {
                Messages.Insert(i + 1, DigestMark(ep));
            }
        }
        EpisodesChanged?.Invoke();
    }

    /// <summary>Stored rows (oldest first) as transcript items with their marks: a day divider
    /// where the day changes (at a user message, so a turn is never split), an episode marker and
    /// digest card where the backend says an episode starts.</summary>
    private List<ChatMessage> BuildThreadItems(List<Dictionary<string, object?>> rows)
    {
        var items = new List<ChatMessage>();
        var segment = new List<Dictionary<string, object?>>();
        DateTime? day = null;

        void Flush()
        {
            if (segment.Count == 0) return;
            foreach (var msg in MapHistory(segment)) items.Add(msg);
            segment.Clear();
        }

        foreach (var row in rows)
        {
            var at = ReadNumber(row, "created_at") is double ts ? FromUnix(ts) : DateTimeOffset.Now;
            var isUser = ReadString(row, "role") == "user";
            var startsEpisode = ReadNumber(row, "episode_start") is double ep ? (long)ep : (long?)null;
            if ((isUser && (day is null || at.Date != day)) || startsEpisode is not null || day is null)
            {
                Flush();
                if (day is null || at.Date != day)
                {
                    items.Add(DayMark(at));
                    day = at.Date;
                }
                if (startsEpisode is long id)
                {
                    var episode = Episodes.FirstOrDefault(e => e.Id == id);
                    items.Add(new ChatMessage
                    {
                        Role = MessageRole.System, Mark = ThreadMark.Episode, EpisodeId = id, CreatedAt = at,
                        MarkText = episode is null ? $"New topic · {at:HH:mm}" : EpisodeLine(episode),
                    });
                    if (episode is not null && !string.IsNullOrWhiteSpace(episode.Digest)) items.Add(DigestMark(episode));
                }
            }
            segment.Add(row);
        }
        Flush();
        return items;
    }

    private static ChatMessage DayMark(DateTimeOffset at) => new()
    {
        Role = MessageRole.System, Mark = ThreadMark.Day, CreatedAt = at, MarkText = DayLabel(at.LocalDateTime),
    };

    private static ChatMessage DigestMark(ThreadEpisode ep) => new()
    {
        Role = MessageRole.System, Mark = ThreadMark.Digest, EpisodeId = ep.Id,
        CreatedAt = new DateTimeOffset(ep.StartedLocal),
        MarkText = ep.Digest!.Contains("This morning's briefing") ? "This morning's briefing and earlier topics" : "Picked up from earlier",
        MarkDetail = DigestBody(ep.Digest!),
    };

    /// <summary>"Today", "Yesterday", "Monday 19 October" -- sentence case, no year this year.</summary>
    public static string DayLabel(DateTime day)
    {
        var today = DateTime.Now.Date;
        if (day.Date == today) return "Today";
        if (day.Date == today.AddDays(-1)) return "Yesterday";
        return day.Year == today.Year ? day.ToString("dddd d MMMM") : day.ToString("dddd d MMMM yyyy");
    }

    /// <summary>"GPU temps and Game mode · 14:02", or "New topic · 14:02" until the record exists.</summary>
    public static string EpisodeLine(ThreadEpisode ep) =>
        $"{(string.IsNullOrWhiteSpace(ep.Title) ? "New topic" : ep.Title)} · {ep.StartedLocal:HH:mm}";

    /// <summary>The digest as the model sees it, without its bracket framing.</summary>
    private static string DigestBody(string digest)
    {
        var lines = digest.Trim().TrimStart('[').TrimEnd(']').Split('\n').Select(l => l.Trim()).Where(l => l.Length > 0).ToList();
        if (lines.Count > 0 && lines[0].StartsWith("Earlier in this conversation", StringComparison.Ordinal)) lines.RemoveAt(0);
        return string.Join("\n", lines);
    }

    private int IndexOfEpisode(long episodeId)
    {
        for (var i = 0; i < Messages.Count; i++)
        {
            if (Messages[i].Mark == ThreadMark.Episode && Messages[i].EpisodeId == episodeId) return i;
        }
        return -1;
    }

    private static long? OldestId(ThreadMessagePage page) =>
        page.Messages.Count > 0 && ReadNumber(page.Messages[0], "id") is double id ? (long)id : null;

    private static double? ReadNumber(Dictionary<string, object?> entry, string key)
    {
        if (!entry.TryGetValue(key, out var value) || value is null) return null;
        return value switch
        {
            JsonElement { ValueKind: JsonValueKind.Number } je => je.GetDouble(),
            double d => d,
            long l => l,
            int i => i,
            _ => double.TryParse(value.ToString(), System.Globalization.NumberStyles.Float,
                                 System.Globalization.CultureInfo.InvariantCulture, out var p) ? p : null,
        };
    }

    private static DateTimeOffset FromUnix(double seconds) =>
        DateTimeOffset.FromUnixTimeMilliseconds((long)(seconds * 1000)).ToLocalTime();

    private Task<T> OnUiAsync<T>(Func<T> read)
    {
        var done = new TaskCompletionSource<T>();
        Post(() =>
        {
            try { done.TrySetResult(read()); }
            catch (Exception ex) { done.TrySetException(ex); }
        });
        return done.Task;
    }
}
