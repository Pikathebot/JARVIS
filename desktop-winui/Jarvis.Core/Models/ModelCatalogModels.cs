using System.Text.Json.Serialization;

namespace Jarvis.Core.Models;

/// <summary>
/// One local GGUF, as reported by GET /api/models. Mirrors backend/app/agent/model_catalog.py's
/// ModelInfo.
/// </summary>
public sealed class ModelInfo
{
    /// <summary>Repo-relative POSIX path, e.g. "models/Qwen3.5-9B-UD-Q3_K_XL.gguf". This is the
    /// value to send back when selecting.</summary>
    public string Id { get; set; } = "";

    /// <summary>Filename without extension, for display.</summary>
    public string Name { get; set; } = "";

    public long SizeBytes { get; set; }

    /// <summary>True for models sitting directly in models/ — the deliberately installed ones,
    /// as opposed to files nested in vendor download trees like models/unsloth/. The backend
    /// already returns these first; a picker should keep that order and default to one.</summary>
    public bool Recommended { get; set; }

    /// <summary>Parameter-count hint parsed from the filename ("9B", "4B"), or null.</summary>
    public string? Family { get; set; }

    /// <summary>Repo-relative directory holding the file, for grouping by source.</summary>
    public string Directory { get; set; } = "";

    /// <summary>Which slots currently point at this model ("main", "fast", or both).</summary>
    public List<string> Slots { get; set; } = new();

    /// <summary>Catalogue id of the mmproj-*.gguf paired with this model (the projector in its
    /// own folder), or null for a text-only model. A model with a projector sees images.</summary>
    public string? Projector { get; set; }

    /// <summary>Human-readable size, e.g. "4.7 GB" — VRAM headroom is the whole question when
    /// picking a model on an 8GB card, so it belongs next to the name.</summary>
    [JsonIgnore]
    public string SizeDisplay => SizeBytes >= 1L << 30
        ? $"{SizeBytes / (double)(1L << 30):0.#} GB"
        : $"{SizeBytes / (double)(1L << 20):0} MB";
}

/// <summary>Response of GET /api/models.</summary>
public sealed class ModelCatalogResponse
{
    public List<ModelInfo> Models { get; set; } = new();

    /// <summary>Multimodal projectors (mmproj-*.gguf). Loaded alongside a model with --mmproj,
    /// never selectable as a chat model — listed so the vision wiring can find them.</summary>
    public List<ModelInfo> Projectors { get; set; } = new();

    /// <summary>Slot name to selected model id; a null value means the slot falls back to the
    /// path configured in .env.</summary>
    public Dictionary<string, string?> Selection { get; set; } = new();

    public List<string> Slots { get; set; } = new();

    /// <summary>Which slot llama-server currently has loaded, or null when nothing is running.</summary>
    public string? LoadedSlot { get; set; }

    /// <summary>True when llama-server was started outside Jarvis — switching models then has to
    /// stop that process to free the port.</summary>
    public bool ExternallyManaged { get; set; }

    /// <summary>Filename of the --mmproj the running server actually loaded, or null when it is
    /// text-only (no projector beside the model, vision disabled, or the projector had to be
    /// dropped for VRAM).</summary>
    public string? LoadedProjector { get; set; }

    /// <summary>The CPU image-captioner sidecar that describes images for a model without a
    /// projector.</summary>
    public CaptionerStatus? Captioner { get; set; }
}

/// <summary>Status of the image-captioner sidecar (backend/app/agent/captioner.py).</summary>
public sealed class CaptionerStatus
{
    public bool Enabled { get; set; }

    /// <summary>Enabled and both the model and its projector exist on disk.</summary>
    public bool Available { get; set; }

    public bool Running { get; set; }

    public string? Model { get; set; }
}

public sealed class ModelSelectRequest
{
    public string Slot { get; set; } = "main";
    public string ModelId { get; set; } = "";

    /// <summary>Restart llama-server onto the model now. False just records the choice.</summary>
    public bool Activate { get; set; } = true;
}

public sealed class ModelSelectResponse
{
    public ModelInfo Selected { get; set; } = new();
    public Dictionary<string, string?> Selection { get; set; } = new();
    public bool Activated { get; set; }

    /// <summary>Set when the model was recorded but would not load — the selection is kept
    /// either way, so this is a warning to surface rather than a failed request.</summary>
    public string? Error { get; set; }
}
