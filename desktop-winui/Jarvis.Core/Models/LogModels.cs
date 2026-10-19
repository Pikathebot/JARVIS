namespace Jarvis.Core.Models;

/// <summary>GET /api/logs (PLAN 4.17): the tail of one log source, filtered server-side.</summary>
public sealed class LogsResponse
{
    public string Source { get; set; } = "";

    /// <summary>The file read (backend.log), or null for in-memory output (model, sidecars).</summary>
    public string? Path { get; set; }

    public List<string> Lines { get; set; } = new();

    /// <summary>Lines that passed the filters, of which the last <c>lines</c> were sent.</summary>
    public int TotalMatched { get; set; }
}
