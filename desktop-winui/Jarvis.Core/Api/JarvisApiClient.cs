using System.Net.Http.Json;
using System.Text.Json;
using Jarvis.Core.Json;
using Jarvis.Core.Models;

namespace Jarvis.Core.Api;

/// <summary>
/// Typed port of desktop-app/src/lib/api.ts (879 lines, ~45 endpoints). One HttpClient against
/// http://127.0.0.1:8000, no auth, no CORS concerns (HttpClient is not a browser). Every method
/// name/shape below mirrors its TS counterpart 1:1 so the mapping stays obvious on review.
/// </summary>
public sealed class JarvisApiClient
{
    public const string DefaultBaseUrl = "http://127.0.0.1:8000";

    private readonly HttpClient _http;

    public JarvisApiClient(HttpClient http)
    {
        _http = http;
        if (_http.BaseAddress is null)
        {
            _http.BaseAddress = new Uri(DefaultBaseUrl);
        }
    }

    public static string CreateNewSessionId()
    {
        var timestamp = ToBase36(DateTimeOffset.UtcNow.ToUnixTimeMilliseconds());
        var random = Guid.NewGuid().ToString("n")[..5];
        return $"session_{timestamp}_{random}";
    }

    /// <summary>.NET's Convert.ToString(long, toBase) only supports base 2/8/10/16, so base36
    /// (matching Date.now().toString(36) in createNewSessionId() in api.ts) needs a manual encode.</summary>
    private static string ToBase36(long value)
    {
        const string digits = "0123456789abcdefghijklmnopqrstuvwxyz";
        if (value == 0) return "0";
        var chars = new Stack<char>();
        var n = value;
        while (n > 0)
        {
            chars.Push(digits[(int)(n % 36)]);
            n /= 36;
        }
        return new string(chars.ToArray());
    }

    // ==========================================
    // Health / sessions / models / governor
    // ==========================================

    public Task<HealthResponse> FetchHealthAsync(CancellationToken ct = default) =>
        GetAsync<HealthResponse>("/health", ct);

    public Task<List<Session>> FetchSessionsAsync(string? projectId = null, CancellationToken ct = default)
    {
        var url = projectId is null ? "/sessions" : $"/sessions?project_id={Uri.EscapeDataString(projectId)}";
        return GetAsync<List<Session>>(url, ct);
    }

    public async Task<List<Dictionary<string, object?>>> FetchSessionMessagesAsync(string sessionId, CancellationToken ct = default)
    {
        var response = await _http.GetAsync($"/sessions/{Uri.EscapeDataString(sessionId)}/messages", ct).ConfigureAwait(false);
        if (response.StatusCode == System.Net.HttpStatusCode.NotFound)
        {
            return new List<Dictionary<string, object?>>();
        }
        await EnsureSuccessAsync(response, "fetch session messages").ConfigureAwait(false);
        return await ReadAsync<List<Dictionary<string, object?>>>(response, ct).ConfigureAwait(false);
    }

    public async Task<bool> DeleteSessionAsync(string sessionId, CancellationToken ct = default)
    {
        var response = await _http.DeleteAsync($"/sessions/{Uri.EscapeDataString(sessionId)}", ct).ConfigureAwait(false);
        return response.IsSuccessStatusCode;
    }

    public Task<UnloadResponse> UnloadModelsAsync(CancellationToken ct = default) =>
        PostAsync<object?, UnloadResponse>("/models/unload", null, ct);

    /// <summary>Every local GGUF, recommended (top-level) ones first — the backend's ordering is
    /// meaningful, so callers should preserve it rather than re-sorting.</summary>
    public Task<ModelCatalogResponse> FetchModelCatalogAsync(CancellationToken ct = default) =>
        GetAsync<ModelCatalogResponse>("/api/models", ct);

    /// <summary>Points a slot ("main" or "fast") at a model. With <paramref name="activate"/>,
    /// llama-server restarts onto it; the returned Error is non-null when the choice was saved
    /// but the model would not load.</summary>
    public Task<ModelSelectResponse> SelectModelAsync(string slot, string modelId, bool activate = true, CancellationToken ct = default) =>
        PostAsync<ModelSelectRequest, ModelSelectResponse>(
            "/api/models/select",
            new ModelSelectRequest { Slot = slot, ModelId = modelId, Activate = activate },
            ct);

    public async Task PauseGovernorAsync(string? reason = null, CancellationToken ct = default)
    {
        var body = new GovernorPauseRequest { Reason = reason ?? "User requested manual pause" };
        await _http.PostAsJsonAsync("/governor/pause", body, JarvisJson.Options, ct).ConfigureAwait(false);
    }

    public async Task ResumeGovernorAsync(CancellationToken ct = default)
    {
        await _http.PostAsync("/governor/resume", content: null, ct).ConfigureAwait(false);
    }

    public Task<GovernorTelemetry> FetchGovernorStatusAsync(CancellationToken ct = default) =>
        GetAsync<GovernorTelemetry>("/governor/status", ct);

    // ==========================================
    // Confirmations
    // ==========================================

    /// <summary>Tells the backend the user declined a pending action, so it stops waiting for an
    /// answer and does not later announce that the confirmation timed out. A 404 (the ask already
    /// lapsed or was never registered) is not an error worth surfacing.</summary>
    public async Task DenyConfirmationAsync(string actionId, CancellationToken ct = default)
    {
        using var response = await _http.PostAsync(
            $"/api/confirmations/{Uri.EscapeDataString(actionId)}/deny", content: null, ct).ConfigureAwait(false);
        if (response.StatusCode != System.Net.HttpStatusCode.NotFound)
        {
            response.EnsureSuccessStatusCode();
        }
    }

    // ==========================================
    // Projects / workspaces
    // ==========================================

    public Task<List<Project>> FetchProjectsAsync(CancellationToken ct = default) =>
        GetAsync<List<Project>>("/api/projects", ct);

    public async Task<Project?> FetchActiveProjectAsync(CancellationToken ct = default)
    {
        var response = await _http.GetAsync("/api/projects/active/current", ct).ConfigureAwait(false);
        if (response.StatusCode == System.Net.HttpStatusCode.NotFound)
        {
            return null;
        }
        await EnsureSuccessAsync(response, "fetch active project").ConfigureAwait(false);
        return await ReadAsync<Project>(response, ct).ConfigureAwait(false);
    }

    public Task<Project> CreateProjectAsync(CreateProjectRequest data, CancellationToken ct = default) =>
        PostAsync<CreateProjectRequest, Project>("/api/projects", data, ct);

    public Task<Project> GetProjectAsync(string projectId, CancellationToken ct = default) =>
        GetAsync<Project>($"/api/projects/{Uri.EscapeDataString(projectId)}", ct);

    public async Task<Project> UpdateProjectAsync(string projectId, UpdateProjectRequest data, CancellationToken ct = default)
    {
        var response = await _http.PutAsJsonAsync($"/api/projects/{Uri.EscapeDataString(projectId)}", data, JarvisJson.Options, ct).ConfigureAwait(false);
        await EnsureSuccessAsync(response, "update project").ConfigureAwait(false);
        return await ReadAsync<Project>(response, ct).ConfigureAwait(false);
    }

    public async Task<bool> DeleteProjectAsync(string projectId, bool cleanFiles = true, CancellationToken ct = default)
    {
        var response = await _http.DeleteAsync($"/api/projects/{Uri.EscapeDataString(projectId)}?clean_files={(cleanFiles ? "true" : "false")}", ct).ConfigureAwait(false);
        return response.IsSuccessStatusCode;
    }

    public Task<Project> ActivateProjectAsync(string projectId, CancellationToken ct = default) =>
        PostAsync<object?, Project>($"/api/projects/{Uri.EscapeDataString(projectId)}/activate", null, ct);

    public async Task<List<ProjectFile>> FetchProjectFilesAsync(string projectId, CancellationToken ct = default)
    {
        var response = await _http.GetAsync($"/api/projects/{Uri.EscapeDataString(projectId)}/files", ct).ConfigureAwait(false);
        if (response.StatusCode == System.Net.HttpStatusCode.NotFound)
        {
            return new List<ProjectFile>();
        }
        await EnsureSuccessAsync(response, "fetch project files").ConfigureAwait(false);
        return await ReadAsync<List<ProjectFile>>(response, ct).ConfigureAwait(false);
    }

    // ==========================================
    // Attachments
    // ==========================================

    public async Task<Attachment> UploadAttachmentAsync(string filePath, string? sessionId = null, string? projectId = null, CancellationToken ct = default)
    {
        using var content = new MultipartFormDataContent();
        await using var stream = File.OpenRead(filePath);
        using var fileContent = new StreamContent(stream);
        content.Add(fileContent, "file", Path.GetFileName(filePath));
        if (sessionId is not null) content.Add(new StringContent(sessionId), "session_id");
        if (projectId is not null) content.Add(new StringContent(projectId), "project_id");

        var response = await _http.PostAsync("/api/upload", content, ct).ConfigureAwait(false);
        await EnsureSuccessAsync(response, "upload attachment").ConfigureAwait(false);
        return await ReadAsync<Attachment>(response, ct).ConfigureAwait(false);
    }

    public Task<List<Attachment>> FetchAttachmentsAsync(string? sessionId = null, string? projectId = null, CancellationToken ct = default)
    {
        var query = BuildQuery(("session_id", sessionId), ("project_id", projectId));
        return GetAsync<List<Attachment>>($"/api/attachments{query}", ct);
    }

    public async Task<bool> DeleteAttachmentAsync(string attachmentId, CancellationToken ct = default)
    {
        var response = await _http.DeleteAsync($"/api/attachments/{Uri.EscapeDataString(attachmentId)}", ct).ConfigureAwait(false);
        return response.IsSuccessStatusCode;
    }

    // ==========================================
    // Artifacts
    // ==========================================

    public Task<List<Artifact>> FetchArtifactsAsync(string? sessionId = null, string? projectId = null, CancellationToken ct = default)
    {
        var query = BuildQuery(("session_id", sessionId), ("project_id", projectId));
        return GetAsync<List<Artifact>>($"/api/artifacts{query}", ct);
    }

    public Task<Artifact> GetArtifactAsync(string artifactId, CancellationToken ct = default) =>
        GetAsync<Artifact>($"/api/artifacts/{Uri.EscapeDataString(artifactId)}", ct);

    public Task<Artifact> CreateArtifactAsync(CreateArtifactRequest data, CancellationToken ct = default) =>
        PostAsync<CreateArtifactRequest, Artifact>("/api/artifacts", data, ct);

    public async Task<Artifact> UpdateArtifactAsync(string artifactId, UpdateArtifactRequest data, CancellationToken ct = default)
    {
        var response = await _http.PutAsJsonAsync($"/api/artifacts/{Uri.EscapeDataString(artifactId)}", data, JarvisJson.Options, ct).ConfigureAwait(false);
        await EnsureSuccessAsync(response, "update artifact").ConfigureAwait(false);
        return await ReadAsync<Artifact>(response, ct).ConfigureAwait(false);
    }

    public async Task<bool> DeleteArtifactAsync(string artifactId, CancellationToken ct = default)
    {
        var response = await _http.DeleteAsync($"/api/artifacts/{Uri.EscapeDataString(artifactId)}", ct).ConfigureAwait(false);
        return response.IsSuccessStatusCode;
    }

    public Task<List<ArtifactVersion>> FetchArtifactVersionsAsync(string artifactId, CancellationToken ct = default) =>
        GetAsync<List<ArtifactVersion>>($"/api/artifacts/{Uri.EscapeDataString(artifactId)}/versions", ct);

    public Task<ArtifactVersion> CreateArtifactVersionAsync(string artifactId, CreateArtifactVersionRequest data, CancellationToken ct = default) =>
        PostAsync<CreateArtifactVersionRequest, ArtifactVersion>($"/api/artifacts/{Uri.EscapeDataString(artifactId)}/versions", data, ct);

    public Task<Artifact> RestoreArtifactVersionAsync(string artifactId, int version, CancellationToken ct = default) =>
        PostAsync<object?, Artifact>($"/api/artifacts/{Uri.EscapeDataString(artifactId)}/restore/{version}", null, ct);

    // ==========================================
    // Memories
    // ==========================================

    public Task<List<MemoryItem>> FetchMemoriesAsync(string? query = null, string? category = null, string? projectId = null, int limit = 20, CancellationToken ct = default)
    {
        var q = BuildQuery(("query", query), ("category", category), ("project_id", projectId), ("limit", limit.ToString()));
        return GetAsync<List<MemoryItem>>($"/api/memories{q}", ct);
    }

    public Task<MemoryItem> CreateMemoryAsync(CreateMemoryRequest data, CancellationToken ct = default) =>
        PostAsync<CreateMemoryRequest, MemoryItem>("/api/memories", data, ct);

    public async Task<bool> DeleteMemoryAsync(string memoryId, CancellationToken ct = default)
    {
        var response = await _http.DeleteAsync($"/api/memories/{Uri.EscapeDataString(memoryId)}", ct).ConfigureAwait(false);
        return response.IsSuccessStatusCode;
    }

    // ==========================================
    // Voice (status/output toggle; hands-free session lives below)
    // ==========================================

    public Task<VoiceStatusResponse> FetchVoiceStatusAsync(CancellationToken ct = default) =>
        GetAsync<VoiceStatusResponse>("/voice/status", ct);

    public async Task<ToggleVoiceOutputResponse> ToggleVoiceOutputAsync(bool enabled, CancellationToken ct = default)
    {
        var response = await _http.PostAsJsonAsync("/voice/output", new { enabled }, ct).ConfigureAwait(false);
        await EnsureSuccessAsync(response, "toggle voice output").ConfigureAwait(false);
        return await ReadAsync<ToggleVoiceOutputResponse>(response, ct).ConfigureAwait(false);
    }

    // ==========================================
    // Persona
    // ==========================================

    public Task<PersonaStatus> FetchPersonaAsync(CancellationToken ct = default) =>
        GetAsync<PersonaStatus>("/api/persona", ct);

    public async Task<PersonaStatus> SetPersonaAsync(string personaId, CancellationToken ct = default)
    {
        var response = await _http.PostAsJsonAsync("/api/persona", new { persona_id = personaId }, ct).ConfigureAwait(false);
        await EnsureSuccessAsync(response, "set persona").ConfigureAwait(false);
        return await ReadAsync<PersonaStatus>(response, ct).ConfigureAwait(false);
    }

    public async Task<PersonaStatus> SetPersonaOverridesAsync(PersonaOverrides overrides, CancellationToken ct = default)
    {
        using var request = new HttpRequestMessage(HttpMethod.Patch, "/api/persona/overrides")
        {
            Content = JsonContent.Create(overrides, options: JarvisJson.Options),
        };
        var response = await _http.SendAsync(request, ct).ConfigureAwait(false);
        await EnsureSuccessAsync(response, "update persona overrides").ConfigureAwait(false);
        return await ReadAsync<PersonaStatus>(response, ct).ConfigureAwait(false);
    }

    public async Task<PersonaStatus> ClearPersonaOverridesAsync(CancellationToken ct = default)
    {
        var response = await _http.DeleteAsync("/api/persona/overrides", ct).ConfigureAwait(false);
        await EnsureSuccessAsync(response, "reset persona overrides").ConfigureAwait(false);
        return await ReadAsync<PersonaStatus>(response, ct).ConfigureAwait(false);
    }

    // ==========================================
    // Hands-free voice session
    // ==========================================

    public async Task<VoiceListenResult> ListenChunkAsync(Stream wavAudio, string sessionId, CancellationToken ct = default)
    {
        using var content = new MultipartFormDataContent();
        using var audioContent = new StreamContent(wavAudio);
        audioContent.Headers.ContentType = new System.Net.Http.Headers.MediaTypeHeaderValue("audio/wav");
        content.Add(audioContent, "file", "utterance.wav");
        content.Add(new StringContent(sessionId), "session_id");

        var response = await _http.PostAsync("/api/voice/listen", content, ct).ConfigureAwait(false);
        await EnsureSuccessAsync(response, "voice listen").ConfigureAwait(false);
        return await ReadAsync<VoiceListenResult>(response, ct).ConfigureAwait(false);
    }

    public Task<VoiceSayResult> SayAsync(string text, string sessionId, string? voiceId = null, CancellationToken ct = default) =>
        PostAsync<VoiceSayRequest, VoiceSayResult>("/api/voice/say", new VoiceSayRequest { Text = text, SessionId = sessionId, VoiceId = voiceId }, ct);

    public Task<VoiceSessionState> StartHandsFreeAsync(string sessionId, CancellationToken ct = default) =>
        VoiceSessionActionAsync(sessionId, "start", ct);

    public Task<VoiceSessionState> StopHandsFreeAsync(string sessionId, CancellationToken ct = default) =>
        VoiceSessionActionAsync(sessionId, "stop", ct);

    public Task<VoiceSessionState> ArmFollowUpAsync(string sessionId, CancellationToken ct = default) =>
        VoiceSessionActionAsync(sessionId, "arm", ct);

    private Task<VoiceSessionState> VoiceSessionActionAsync(string sessionId, string action, CancellationToken ct) =>
        PostAsync<object?, VoiceSessionState>($"/api/voice/session/{Uri.EscapeDataString(sessionId)}/{action}", null, ct);

    public Task<VoiceSessionState> SetVoiceStateAsync(string sessionId, VoiceState state, CancellationToken ct = default) =>
        PostAsync<VoiceStateRequest, VoiceSessionState>($"/api/voice/session/{Uri.EscapeDataString(sessionId)}/state", new VoiceStateRequest { State = state }, ct);

    public Task<HandsFreeStatus> FetchHandsFreeStatusAsync(CancellationToken ct = default) =>
        GetAsync<HandsFreeStatus>("/api/voice/hands-free", ct);

    // ==========================================
    // Ambient awareness
    // ==========================================

    public Task<AwarenessStatus> FetchAwarenessStatusAsync(CancellationToken ct = default) =>
        GetAsync<AwarenessStatus>("/api/awareness/status", ct);

    public Task<ObservationsPage> FetchObservationsAsync(long sinceSeq = 0, int limit = 20, CancellationToken ct = default) =>
        GetAsync<ObservationsPage>($"/api/awareness/observations?since_seq={sinceSeq}&limit={limit}", ct);

    public async Task AcknowledgeObservationAsync(string observationId, CancellationToken ct = default)
    {
        var response = await _http.PostAsync($"/api/awareness/observations/{Uri.EscapeDataString(observationId)}/ack", content: null, ct).ConfigureAwait(false);
        await EnsureSuccessAsync(response, "acknowledge observation").ConfigureAwait(false);
    }

    public Task<Briefing> FetchBriefingAsync(CancellationToken ct = default) =>
        GetAsync<Briefing>("/api/awareness/briefing", ct);

    public string AwarenessStreamUrl => "/api/awareness/stream";

    public async Task<AwarenessMonitorStatus> UpdateAwarenessConfigAsync(AwarenessConfigPatch patch, CancellationToken ct = default)
    {
        using var request = new HttpRequestMessage(HttpMethod.Patch, "/api/awareness/config")
        {
            Content = JsonContent.Create(patch, options: JarvisJson.Options),
        };
        var response = await _http.SendAsync(request, ct).ConfigureAwait(false);
        await EnsureSuccessAsync(response, "update awareness config").ConfigureAwait(false);
        return await ReadAsync<AwarenessMonitorStatus>(response, ct).ConfigureAwait(false);
    }

    // ==========================================
    // Scheduled routines
    // ==========================================

    public Task<RoutinesResponse> FetchRoutinesAsync(CancellationToken ct = default) =>
        GetAsync<RoutinesResponse>("/api/routines", ct);

    public Task<Routine> CreateRoutineAsync(RoutineInput input, CancellationToken ct = default) =>
        PostAsync<RoutineInput, Routine>("/api/routines", input, ct);

    public async Task<Routine> UpdateRoutineAsync(string routineId, RoutineInput input, CancellationToken ct = default)
    {
        using var request = new HttpRequestMessage(HttpMethod.Patch, $"/api/routines/{Uri.EscapeDataString(routineId)}")
        {
            Content = JsonContent.Create(input, options: JarvisJson.Options),
        };
        var response = await _http.SendAsync(request, ct).ConfigureAwait(false);
        await EnsureSuccessAsync(response, "update routine").ConfigureAwait(false);
        return await ReadAsync<Routine>(response, ct).ConfigureAwait(false);
    }

    public async Task DeleteRoutineAsync(string routineId, CancellationToken ct = default)
    {
        var response = await _http.DeleteAsync($"/api/routines/{Uri.EscapeDataString(routineId)}", ct).ConfigureAwait(false);
        await EnsureSuccessAsync(response, "delete routine").ConfigureAwait(false);
    }

    public Task<Observation> RunRoutineNowAsync(string routineId, CancellationToken ct = default) =>
        PostAsync<object?, Observation>($"/api/routines/{Uri.EscapeDataString(routineId)}/run", null, ct);

    // ==========================================
    // Non-streaming chat (HUD)
    // ==========================================

    public Task<ChatResponse> SendChatAsync(SendChatRequest data, CancellationToken ct = default) =>
        PostAsync<SendChatRequest, ChatResponse>("/chat", data, ct);

    // ==========================================
    // Helpers
    // ==========================================

    private async Task<T> GetAsync<T>(string url, CancellationToken ct)
    {
        var response = await _http.GetAsync(url, ct).ConfigureAwait(false);
        await EnsureSuccessAsync(response, $"GET {url}").ConfigureAwait(false);
        return await ReadAsync<T>(response, ct).ConfigureAwait(false);
    }

    private async Task<TResponse> PostAsync<TRequest, TResponse>(string url, TRequest? body, CancellationToken ct)
    {
        var response = await _http.PostAsJsonAsync(url, body, JarvisJson.Options, ct).ConfigureAwait(false);
        await EnsureSuccessAsync(response, $"POST {url}").ConfigureAwait(false);
        return await ReadAsync<TResponse>(response, ct).ConfigureAwait(false);
    }

    private static async Task EnsureSuccessAsync(HttpResponseMessage response, string action)
    {
        if (response.IsSuccessStatusCode)
        {
            return;
        }

        string? detail = null;
        try
        {
            var text = await response.Content.ReadAsStringAsync().ConfigureAwait(false);
            if (!string.IsNullOrWhiteSpace(text))
            {
                using var doc = JsonDocument.Parse(text);
                if (doc.RootElement.TryGetProperty("detail", out var detailProp))
                {
                    detail = detailProp.GetString();
                }
            }
        }
        catch
        {
            // best-effort only
        }

        throw new ApiException(detail ?? $"Failed to {action}: HTTP {(int)response.StatusCode}", (int)response.StatusCode);
    }

    private static async Task<T> ReadAsync<T>(HttpResponseMessage response, CancellationToken ct)
    {
        var result = await response.Content.ReadFromJsonAsync<T>(JarvisJson.Options, ct).ConfigureAwait(false);
        return result ?? throw new ApiException("Empty response body");
    }

    private static string BuildQuery(params (string Key, string? Value)[] parts)
    {
        var present = parts.Where(p => !string.IsNullOrEmpty(p.Value)).ToList();
        if (present.Count == 0)
        {
            return "";
        }
        var joined = string.Join('&', present.Select(p => $"{p.Key}={Uri.EscapeDataString(p.Value!)}"));
        return $"?{joined}";
    }
}
