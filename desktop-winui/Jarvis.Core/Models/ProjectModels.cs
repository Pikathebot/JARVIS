using System.Text.Json.Serialization;

namespace Jarvis.Core.Models;

public sealed class Project
{
    public string Id { get; set; } = "";
    public string Name { get; set; } = "";
    public string? Description { get; set; }
    public string? Instructions { get; set; }

    [JsonPropertyName("workspace_path")]
    public string? WorkspacePath { get; set; }

    [JsonPropertyName("local_folders")]
    public List<string> LocalFolders { get; set; } = new();

    [JsonPropertyName("is_active")]
    public bool IsActive { get; set; }

    [JsonPropertyName("created_at")]
    public string CreatedAt { get; set; } = "";

    [JsonPropertyName("updated_at")]
    public string UpdatedAt { get; set; } = "";
}

public sealed class Session
{
    [JsonPropertyName("session_id")]
    public string SessionId { get; set; } = "";

    [JsonPropertyName("project_id")]
    public string? ProjectId { get; set; }

    [JsonPropertyName("title")]
    public string? Title { get; set; }

    [JsonPropertyName("chat_mode")]
    public string? ChatMode { get; set; }

    // Unix timestamps (float seconds) on the wire -- typed as strings these failed to
    // deserialize and the sidebar's session list silently stayed empty.
    [JsonPropertyName("created_at")]
    public double? CreatedAt { get; set; }

    [JsonPropertyName("updated_at")]
    public double? UpdatedAt { get; set; }

    [JsonPropertyName("message_count")]
    public int? MessageCount { get; set; }

    [JsonPropertyName("last_message")]
    public string? LastMessage { get; set; }
}

public sealed class Attachment
{
    public string Id { get; set; } = "";

    [JsonPropertyName("session_id")]
    public string? SessionId { get; set; }

    [JsonPropertyName("project_id")]
    public string? ProjectId { get; set; }

    public string Filename { get; set; } = "";
    public string Path { get; set; } = "";

    [JsonPropertyName("size_bytes")]
    public long SizeBytes { get; set; }

    [JsonPropertyName("content_type")]
    public string? ContentType { get; set; }

    [JsonPropertyName("created_at")]
    public string CreatedAt { get; set; } = "";
}

public sealed class ArtifactVersion
{
    public string Id { get; set; } = "";

    [JsonPropertyName("artifact_id")]
    public string ArtifactId { get; set; } = "";

    public int Version { get; set; }
    public string Content { get; set; } = "";
    public string? Summary { get; set; }

    [JsonPropertyName("created_at")]
    public string CreatedAt { get; set; } = "";
}

public sealed class Artifact
{
    public string Id { get; set; } = "";

    [JsonPropertyName("project_id")]
    public string? ProjectId { get; set; }

    [JsonPropertyName("session_id")]
    public string? SessionId { get; set; }

    public string Name { get; set; } = "";
    public string Type { get; set; } = "";
    public string Content { get; set; } = "";
    public int Version { get; set; }

    [JsonPropertyName("created_at")]
    public string CreatedAt { get; set; } = "";

    [JsonPropertyName("updated_at")]
    public string UpdatedAt { get; set; } = "";
}

public sealed class ProjectFile
{
    public string Name { get; set; } = "";
    public string Path { get; set; } = "";

    [JsonPropertyName("size_bytes")]
    public long SizeBytes { get; set; }

    [JsonPropertyName("is_dir")]
    public bool IsDir { get; set; }

    [JsonPropertyName("updated_at")]
    public double? UpdatedAt { get; set; }
}

public sealed class MemoryItem
{
    public string Id { get; set; } = "";

    [JsonPropertyName("project_id")]
    public string? ProjectId { get; set; }

    public string Category { get; set; } = "";
    public string Content { get; set; } = "";

    [JsonPropertyName("source_session_id")]
    public string? SourceSessionId { get; set; }

    public double Confidence { get; set; }
    public bool Pinned { get; set; }
    public double? Score { get; set; }

    [JsonPropertyName("created_at")]
    public string? CreatedAt { get; set; }

    [JsonPropertyName("updated_at")]
    public string? UpdatedAt { get; set; }
}
