using System.Text.Json;
using Jarvis.Core.Json;
using Jarvis.Core.Models;

namespace Jarvis.Core.Tests;

/// <summary>Payloads shaped like what the backend emits (key names taken from
/// backend/app/agent/orchestrator.py's turn metrics and the SSE done / confirmation events).</summary>
public class DtoRoundTripTests
{
    private const string DoneJson =
        "{\"response\":\"Saved.\",\"reasoning\":\"thinking\",\"model\":\"main\",\"provider\":\"llama_cpp\",\"status\":\"ok\"," +
        "\"session_id\":\"s1\",\"route_reason\":\"heuristic\",\"fallback_used\":false,\"compaction_performed\":true," +
        "\"active_skills\":[\"notes\"],\"tools_used\":[{\"name\":\"write_note\",\"ok\":true}],\"replace_content\":false," +
        "\"metrics\":{\"rounds\":2,\"tools\":1,\"ttft_ms\":410,\"tokens_per_second\":27.5,\"prompt_tokens\":1600," +
        "\"cached_tokens\":1400,\"peak_tokens\":1900,\"nets\":[\"nudged\"]},\"compare_id\":null,\"turn_id\":\"t-9\"}";

    [Fact]
    public void Done_event_reads_every_field_the_backend_sends()
    {
        var done = JsonSerializer.Deserialize<SseDoneEvent>(DoneJson, JarvisJson.Options)!;

        Assert.Equal("Saved.", done.Response);
        Assert.Equal("s1", done.SessionId);
        Assert.Equal("heuristic", done.RouteReason);
        Assert.True(done.CompactionPerformed);
        Assert.Equal(new[] { "notes" }, done.ActiveSkills);
        Assert.Equal("t-9", done.TurnId);
        Assert.Null(done.CompareId);
        Assert.Single(done.ToolsUsed);
        Assert.NotNull(done.Metrics);
        Assert.Equal(2, done.Metrics!.Rounds);
        Assert.Equal(410, done.Metrics.TtftMs);
        Assert.Equal(27.5, done.Metrics.TokensPerSecond);
        Assert.Equal(1400, done.Metrics.CachedTokens);
        Assert.Equal(new[] { "nudged" }, done.Metrics.Nets);
    }

    [Fact]
    public void Done_event_survives_a_write_and_read_with_snake_case_keys()
    {
        var done = JsonSerializer.Deserialize<SseDoneEvent>(DoneJson, JarvisJson.Options)!;

        var json = JsonSerializer.Serialize(done, JarvisJson.Options);
        var again = JsonSerializer.Deserialize<SseDoneEvent>(json, JarvisJson.Options)!;

        Assert.Contains("\"session_id\":\"s1\"", json);
        Assert.Contains("\"ttft_ms\":410", json);
        Assert.Contains("\"tokens_per_second\":27.5", json);
        Assert.DoesNotContain("\"SessionId\"", json);
        Assert.Equal(done.Response, again.Response);
        Assert.Equal(done.Metrics!.TokensPerSecond, again.Metrics!.TokensPerSecond);
        Assert.Equal(done.TurnId, again.TurnId);
    }

    [Fact]
    public void Done_event_tolerates_missing_optional_fields_and_unknown_ones()
    {
        var done = JsonSerializer.Deserialize<SseDoneEvent>(
            "{\"response\":\"hi\",\"added_in_the_future\":123}", JarvisJson.Options)!;

        Assert.Equal("hi", done.Response);
        Assert.Null(done.Metrics);
        Assert.Null(done.TurnId);
        Assert.Empty(done.ActiveSkills);
    }

    [Theory]
    [InlineData("{\"path\":\"a.txt\"}", JsonValueKind.Object)]
    [InlineData("\"git push origin main\"", JsonValueKind.String)]
    public void Pending_confirmation_args_may_be_an_object_or_a_preformatted_string(string args, JsonValueKind kind)
    {
        var json = "{\"action_id\":\"act_1\",\"tool\":\"execute_command\",\"args\":" + args + ",\"risk_tier\":\"HIGH_RISK\"}";

        var card = JsonSerializer.Deserialize<PendingConfirmation>(json, JarvisJson.Options)!;

        Assert.Equal("act_1", card.ActionId);
        Assert.Equal("HIGH_RISK", card.RiskTier);
        Assert.Equal(kind, card.ArgsRaw.ValueKind);
    }

    [Fact]
    public void Chat_request_serializes_with_the_names_the_backend_validates()
    {
        var json = JsonSerializer.Serialize(new ChatRequest
        {
            Message = "hi",
            SessionId = "s1",
            ChatMode = "WORKSPACE",
            ApprovedActionIds = new List<string> { "act_1" },
        }, JarvisJson.Options);

        Assert.Contains("\"message\":\"hi\"", json);
        Assert.Contains("\"session_id\":\"s1\"", json);
        Assert.Contains("\"chat_mode\":\"WORKSPACE\"", json);
        Assert.Contains("\"approved_action_ids\":[\"act_1\"]", json);
        Assert.DoesNotContain("null", json); // unset optionals are left out, not sent as null
    }
}
