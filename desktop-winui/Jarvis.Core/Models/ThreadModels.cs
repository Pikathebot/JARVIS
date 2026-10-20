namespace Jarvis.Core.Models;

/// <summary>One episode of the Freeform thread (PLAN 4.18): <c>GET /api/thread/episodes</c>.
/// <see cref="RecordStatus"/> is "open" (the current one), "pending" (closed, record not
/// written yet) or "done".</summary>
public sealed class ThreadEpisode
{
    public long Id { get; set; }
    public double StartedAt { get; set; }
    public double? EndedAt { get; set; }
    public long StartAfterMessageId { get; set; }
    public long? FoldUptoMessageId { get; set; }
    public string? Digest { get; set; }
    public string? Title { get; set; }
    public string? Summary { get; set; }
    public string? Decisions { get; set; }
    public string? OpenItems { get; set; }
    public string RecordStatus { get; set; } = "open";

    public DateTime StartedLocal => DateTimeOffset.FromUnixTimeMilliseconds((long)(StartedAt * 1000)).LocalDateTime;
}

public sealed class ThreadEpisodesResponse
{
    public List<ThreadEpisode> Episodes { get; set; } = new();
}

/// <summary>A page of the thread's messages, oldest first, each a raw stored row with
/// <c>id</c> and <c>created_at</c>: <c>GET /api/thread/messages</c>.</summary>
public sealed class ThreadMessagePage
{
    public List<Dictionary<string, object?>> Messages { get; set; } = new();
    public bool HasMore { get; set; }
}
