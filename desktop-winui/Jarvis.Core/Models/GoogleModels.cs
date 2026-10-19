using System.Text.Json;

namespace Jarvis.Core.Models;

/// <summary>GET /api/google/status (PLAN 4.16 phase B): the sign-in and when each sync last ran.</summary>
public sealed class GoogleStatus
{
    /// <summary>The user's OAuth client file (data/google_client_secret.json) is in place.</summary>
    public bool Configured { get; set; }
    public bool SignedIn { get; set; }
    public List<string> Scopes { get; set; } = new();

    /// <summary>A browser sign-in is waiting for Google's redirect (up to five minutes).</summary>
    public bool SigningIn { get; set; }

    /// <summary>Why the last sign-in ended without a token; "" when it worked or none ran.</summary>
    public string LastError { get; set; } = "";

    /// <summary>Epoch seconds; null = never synced.</summary>
    public double? TasksSyncedAt { get; set; }
    public double? CalendarSyncedAt { get; set; }
}

/// <summary>POST /api/google/sync: per side, counts of what moved or {"error": "..."}.</summary>
public sealed class GoogleSyncResult
{
    public Dictionary<string, JsonElement> Tasks { get; set; } = new();
    public Dictionary<string, JsonElement> Calendar { get; set; } = new();
    public double? TasksSyncedAt { get; set; }
    public double? CalendarSyncedAt { get; set; }
}
