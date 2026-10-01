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
}
