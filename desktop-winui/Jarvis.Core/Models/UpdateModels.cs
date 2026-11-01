namespace Jarvis.Core.Models;

/// <summary>GET /api/updates (backend/app/updates/manager.py): llama.cpp and the voice packages.</summary>
public sealed class UpdatesStatus
{
    public List<ToolUpdate> Tools { get; set; } = new();

    public UpdateJob? Job { get; set; }

    /// <summary>Unix seconds of the last GitHub/PyPI check.</summary>
    public double? CheckedAt { get; set; }
}

public sealed class ToolUpdate
{
    public string Id { get; set; } = "";

    /// <summary>"llama" (one build) or "packages" (a venv's package rows).</summary>
    public string Kind { get; set; } = "";

    public string Name { get; set; } = "";

    public string Detail { get; set; } = "";

    /// <summary>"build 11899"; null for a package set.</summary>
    public string? Installed { get; set; }

    public string? Latest { get; set; }

    public bool UpdateAvailable { get; set; }

    public long? DownloadSize { get; set; }

    public string? ReleasePage { get; set; }

    /// <summary>What a roll back would restore ("build 11899", "laya 0.3.23"), or null.</summary>
    public string? RollBackTo { get; set; }

    /// <summary>An install queued for the next start, in words; null when none.</summary>
    public string? Pending { get; set; }

    public List<PackageVersion> Packages { get; set; } = new();

    public string? Error { get; set; }

    public UpdateResult? Last { get; set; }
}

public sealed class PackageVersion
{
    public string Name { get; set; } = "";

    public string? Installed { get; set; }

    public string? Latest { get; set; }

    /// <summary>Kept at its installed version (the CUDA torch build).</summary>
    public bool Held { get; set; }

    public bool Outdated { get; set; }
}

public sealed class UpdateResult
{
    public double At { get; set; }

    public bool Ok { get; set; }

    public string Message { get; set; } = "";
}

public sealed class UpdateJob
{
    public string Id { get; set; } = "";

    public string Tool { get; set; } = "";

    /// <summary>"update" or "rollback".</summary>
    public string Action { get; set; } = "";

    /// <summary>"running", "done", "failed" or "cancelled".</summary>
    public string Status { get; set; } = "";

    public string Stage { get; set; } = "";

    public long BytesDone { get; set; }

    public long BytesTotal { get; set; }

    public string? Message { get; set; }
}

public sealed class UpdateStartRequest
{
    public string Action { get; set; } = "update";
}

public sealed class UpdateJobResponse
{
    public UpdateJob? Job { get; set; }
}
