using Jarvis.Core.Models;
using Jarvis_App.ViewModels;

namespace Jarvis_App;

/// <summary>One date group of the sidebar's session list ("Today", "Yesterday", ...): the
/// items are the sessions, <see cref="Key"/> is the header. A List subclass because a grouped
/// CollectionViewSource reads each group as an enumerable of its items.</summary>
public sealed class SessionGroup : List<Session>
{
    public SessionGroup(string key, IEnumerable<Session> sessions) : base(sessions) => Key = key;

    public string Key { get; }

    /// <summary>Groups sessions by when they were last touched, newest group first, keeping
    /// the incoming order inside each group, and drops those that don't match
    /// <paramref name="query"/> (title and last message, case-insensitive).</summary>
    public static List<SessionGroup> Build(IEnumerable<Session> sessions, string? query, DateTime now)
    {
        var today = now.Date;
        var matching = string.IsNullOrWhiteSpace(query)
            ? sessions
            : sessions.Where(s => SessionsViewModel.DisplayLabel(s).Contains(query.Trim(), StringComparison.OrdinalIgnoreCase)
                                  || (s.LastMessage?.Contains(query.Trim(), StringComparison.OrdinalIgnoreCase) ?? false));

        string KeyOf(Session s)
        {
            var stamp = s.UpdatedAt ?? s.CreatedAt;
            if (stamp is null) return "Older";
            var day = DateTimeOffset.FromUnixTimeMilliseconds((long)(stamp.Value * 1000)).LocalDateTime.Date;
            if (day >= today) return "Today";
            if (day >= today.AddDays(-1)) return "Yesterday";
            if (day >= today.AddDays(-7)) return "Previous 7 days";
            return "Older";
        }

        var order = new[] { "Today", "Yesterday", "Previous 7 days", "Older" };
        var byKey = matching.GroupBy(KeyOf).ToDictionary(g => g.Key, g => g.ToList());
        return order.Where(byKey.ContainsKey).Select(k => new SessionGroup(k, byKey[k])).ToList();
    }

    /// <summary>The row id of an episode in the thread's table of contents.</summary>
    public const string EpisodeRowPrefix = ChatViewModel.ThreadId + "#";

    /// <summary>The episode a table-of-contents row stands for, or null for a real session.</summary>
    public static long? EpisodeOf(Session row) =>
        row.SessionId.StartsWith(EpisodeRowPrefix, StringComparison.Ordinal)
        && long.TryParse(row.SessionId.AsSpan(EpisodeRowPrefix.Length), out var id) ? id : null;

    /// <summary>Freeform's sidebar as the thread's table of contents (PLAN 4.18): the
    /// conversation itself, its episodes grouped by the day they started (newest first, each a
    /// row that scrolls the thread to it), then the side chats. The search matches episode titles
    /// and summaries as well as the side chats.</summary>
    public static List<SessionGroup> BuildThread(IEnumerable<ThreadEpisode> episodes, IEnumerable<Session> sideChats,
                                                 string? query, DateTime now)
    {
        var q = query?.Trim() ?? "";
        bool Matches(params string?[] fields) =>
            q.Length == 0 || fields.Any(f => f?.Contains(q, StringComparison.OrdinalIgnoreCase) ?? false);

        var groups = new List<SessionGroup>();
        if (q.Length == 0)
        {
            groups.Add(new SessionGroup("Conversation", new[]
            {
                new Session { SessionId = ChatViewModel.ThreadId, ChatMode = "FREEFORM", LastMessage = "Current conversation" },
            }));
        }
        foreach (var day in episodes.Where(e => Matches(e.Title, e.Summary))
                                    .GroupBy(e => e.StartedLocal.Date)
                                    .OrderByDescending(g => g.Key))
        {
            groups.Add(new SessionGroup(ChatViewModel.DayLabel(day.Key), day
                .OrderByDescending(e => e.StartedLocal)
                .Select(e => new Session
                {
                    SessionId = EpisodeRowPrefix + e.Id, ChatMode = "FREEFORM", LastMessage = ChatViewModel.EpisodeLine(e),
                })));
        }
        var side = sideChats.Where(s => Matches(SessionsViewModel.DisplayLabel(s), s.LastMessage)).ToList();
        if (side.Count > 0) groups.Add(new SessionGroup("Side chats", side));
        return groups;
    }
}
