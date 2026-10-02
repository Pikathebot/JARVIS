using System.Text.Json.Serialization;

namespace Jarvis.Core.Models;

/// <summary>
/// How one slot would take a model, from the "fit" blocks of GET /api/models and the hub
/// routes (backend/app/agent/model_hub.py <c>slot_fits</c>).
/// </summary>
public sealed class SlotFit
{
    /// <summary>The slot's context size the cost was worked out at.</summary>
    public int Ctx { get; set; }

    public double CostMb { get; set; }

    /// <summary>"measured" (a launch on this machine) or "estimate".</summary>
    public string Source { get; set; } = "";

    /// <summary>"fits" (500 MB to spare), "tight", "spills" or "unknown".</summary>
    public string Label { get; set; } = "unknown";

    public static string Describe(string label) => label switch
    {
        "fits" => "✓ fits",
        "tight" => "~ tight",
        "spills" => "⚠ spills",
        _ => "? unknown",
    };

    /// <summary>"Main ✓ fits · Fast ~ tight" for a slot -> fit map.</summary>
    public static string Line(Dictionary<string, SlotFit>? fits)
    {
        if (fits is null || fits.Count == 0) return "";
        var parts = new List<string>();
        foreach (var slot in new[] { "main", "fast" })
        {
            if (fits.TryGetValue(slot, out var fit))
            {
                parts.Add($"{(slot == "main" ? "Main" : "Fast")} {Describe(fit.Label)}");
            }
        }
        return string.Join(" · ", parts);
    }
}

/// <summary>Fit of a file not yet downloaded: text-only, and with the repo's projector when
/// it has one.</summary>
public sealed class HubFit
{
    public Dictionary<string, SlotFit> Text { get; set; } = new();
    public Dictionary<string, SlotFit>? Vision { get; set; }
}

/// <summary>One model (or projector) file in a HuggingFace repo; a split set is one entry
/// named by its first part.</summary>
public sealed class HubFileInfo
{
    public string Name { get; set; } = "";
    public long SizeBytes { get; set; }
    public int Parts { get; set; } = 1;
    public bool Projector { get; set; }
    public bool Downloaded { get; set; }
    public HubFit? Fit { get; set; }

    [JsonIgnore]
    public string SizeDisplay => SizeFormat.Bytes(SizeBytes) + (Parts > 1 ? $" in {Parts} parts" : "");

    /// <summary>The fit with the projector when the user downloads with vision, else
    /// text-only. Set by the view model when the vision switch changes.</summary>
    [JsonIgnore]
    public bool WithVision { get; set; }

    [JsonIgnore]
    public string FitDisplay => Fit is null ? "" : SlotFit.Line(WithVision && Fit.Vision is not null ? Fit.Vision : Fit.Text);

    [JsonIgnore]
    public string DownloadLabel => Downloaded ? "Downloaded" : "Download";

    [JsonIgnore]
    public bool CanDownload => !Downloaded;
}

public sealed class CuratedModel
{
    public string Repo { get; set; } = "";
    public string? Slot { get; set; }
    public string? Note { get; set; }
    public HubFileInfo File { get; set; } = new();
    public HubFileInfo? Projector { get; set; }
    public bool Downloaded { get; set; }
    public HubFit? Fit { get; set; }

    [JsonIgnore]
    public string Subtitle => $"{SizeFormat.Bytes(File.SizeBytes + (Projector?.SizeBytes ?? 0))} · {Repo}"
        + (string.IsNullOrEmpty(Note) ? "" : $"\n{Note}");

    /// <summary>A curated entry downloads with its projector, so its fit is the vision one.</summary>
    [JsonIgnore]
    public string FitDisplay => Fit is null ? "" : SlotFit.Line(Fit.Vision ?? Fit.Text);

    [JsonIgnore]
    public string DownloadLabel => Downloaded ? "Downloaded" : "Download";

    [JsonIgnore]
    public bool CanDownload => !Downloaded;
}

public sealed class CuratedModelsResponse
{
    public List<CuratedModel> Models { get; set; } = new();
    public double? FitBudgetMb { get; set; }
}

public sealed class HubSearchResult
{
    public string Repo { get; set; } = "";
    public long Downloads { get; set; }
    public long Likes { get; set; }

    [JsonIgnore]
    public string Subtitle => $"{Downloads:N0} downloads · {Likes:N0} likes";
}

public sealed class HubSearchResponse
{
    public List<HubSearchResult> Results { get; set; } = new();
}

public sealed class HubFilesResponse
{
    public string Repo { get; set; } = "";

    [JsonPropertyName("kv_mib_per_1k")]
    public double? KvMibPer1k { get; set; }

    public double? FitBudgetMb { get; set; }
    public List<HubFileInfo> Models { get; set; } = new();
    public List<HubFileInfo> Projectors { get; set; } = new();
}

public sealed class ModelDownloadRequest
{
    public string Repo { get; set; } = "";
    public string File { get; set; } = "";
    public string? Projector { get; set; }
}

/// <summary>One download job (GET /api/models/downloads).</summary>
public sealed class ModelDownloadJob
{
    public string Id { get; set; } = "";
    public string Repo { get; set; } = "";
    public List<string> Files { get; set; } = new();
    public string DestDir { get; set; } = "";

    /// <summary>queued | downloading | verifying | done | failed | cancelled</summary>
    public string Status { get; set; } = "";
    public long BytesDone { get; set; }
    public long BytesTotal { get; set; }
    public string? CurrentFile { get; set; }
    public string? Error { get; set; }

    [JsonIgnore]
    public bool IsActive => Status is "queued" or "downloading" or "verifying";
}

public sealed class ModelDownloadResponse
{
    public ModelDownloadJob Download { get; set; } = new();
}

public sealed class ModelDownloadsResponse
{
    public List<ModelDownloadJob> Downloads { get; set; } = new();
}

public static class SizeFormat
{
    public static string Bytes(long bytes) => bytes >= 1L << 30
        ? $"{bytes / (double)(1L << 30):0.#} GB"
        : $"{bytes / (double)(1L << 20):0} MB";
}
