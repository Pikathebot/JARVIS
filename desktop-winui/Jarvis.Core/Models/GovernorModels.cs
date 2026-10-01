using System.Text.Json.Serialization;

namespace Jarvis.Core.Models;

public sealed class HealthResponse
{
    /// <summary>"ok" | "degraded"</summary>
    public string Status { get; set; } = "";

    [JsonPropertyName("active_backend")]
    public string ActiveBackend { get; set; } = "";

    [JsonPropertyName("configured_model")]
    public string ConfiguredModel { get; set; } = "";

    [JsonPropertyName("llama_base_url")]
    public string? LlamaBaseUrl { get; set; }

    [JsonPropertyName("llama_connected")]
    public bool LlamaConnected { get; set; }

    [JsonPropertyName("available_models")]
    public List<string> AvailableModels { get; set; } = new();

    [JsonPropertyName("governor_throttled")]
    public bool GovernorThrottled { get; set; }

    [JsonPropertyName("openrouter_configured")]
    public bool? OpenrouterConfigured { get; set; }

    [JsonPropertyName("active_sessions_count")]
    public int ActiveSessionsCount { get; set; }

    [JsonPropertyName("active_mcp_servers_count")]
    public int? ActiveMcpServersCount { get; set; }

    [JsonPropertyName("available_skills_count")]
    public int? AvailableSkillsCount { get; set; }

    [JsonPropertyName("voice_enabled")]
    public bool? VoiceEnabled { get; set; }

    /// <summary>The backend process, granted the right to raise windows (focus_app).</summary>
    public int Pid { get; set; }

    [JsonPropertyName("ollama_connected")]
    public bool? OllamaConnected { get; set; }

    [JsonPropertyName("ollama_model")]
    public string? OllamaModel { get; set; }
}

public sealed class GovernorMetrics
{
    [JsonPropertyName("cpu_percent")]
    public double CpuPercent { get; set; }

    [JsonPropertyName("ram_percent")]
    public double RamPercent { get; set; }

    [JsonPropertyName("ram_used_mb")]
    public double RamUsedMb { get; set; }

    [JsonPropertyName("ram_total_mb")]
    public double RamTotalMb { get; set; }

    [JsonPropertyName("gpu_available")]
    public bool GpuAvailable { get; set; }

    [JsonPropertyName("gpu_name")]
    public string? GpuName { get; set; }

    [JsonPropertyName("gpu_util_percent")]
    public double GpuUtilPercent { get; set; }

    [JsonPropertyName("vram_util_percent")]
    public double VramUtilPercent { get; set; }

    [JsonPropertyName("vram_used_mb")]
    public double VramUsedMb { get; set; }

    [JsonPropertyName("vram_total_mb")]
    public double VramTotalMb { get; set; }

    [JsonPropertyName("vram_free_mb")]
    public double VramFreeMb { get; set; }

    [JsonPropertyName("gpu_temp_c")]
    public double? GpuTempC { get; set; }

    public double Timestamp { get; set; }
}

public sealed class GovernorThresholds
{
    [JsonPropertyName("gpu_threshold")]
    public double GpuThreshold { get; set; }

    [JsonPropertyName("vram_threshold")]
    public double VramThreshold { get; set; }

    [JsonPropertyName("cpu_threshold")]
    public double CpuThreshold { get; set; }

    [JsonPropertyName("ram_threshold")]
    public double RamThreshold { get; set; }
}

public sealed class GovernorTelemetry
{
    public bool Enabled { get; set; }
    public string Status { get; set; } = "";
    public bool Throttled { get; set; }

    [JsonPropertyName("raw_throttled")]
    public bool RawThrottled { get; set; }

    [JsonPropertyName("throttle_reasons")]
    public List<string> ThrottleReasons { get; set; } = new();

    [JsonPropertyName("is_manual_override")]
    public bool IsManualOverride { get; set; }

    [JsonPropertyName("manual_override_active")]
    public bool ManualOverrideActive { get; set; }

    [JsonPropertyName("override_expires_at")]
    public double? OverrideExpiresAt { get; set; }

    [JsonPropertyName("pending_reload")]
    public bool PendingReload { get; set; }

    [JsonPropertyName("active_activities")]
    public List<string> ActiveActivities { get; set; } = new();

    [JsonPropertyName("model_unloaded")]
    public bool ModelUnloaded { get; set; }

    public GovernorMetrics Metrics { get; set; } = new();
    public GovernorThresholds Thresholds { get; set; } = new();
}

public sealed class UnloadResponse
{
    public bool Success { get; set; }

    [JsonPropertyName("unloaded_models")]
    public List<string> UnloadedModels { get; set; } = new();

    public string Message { get; set; } = "";
}
