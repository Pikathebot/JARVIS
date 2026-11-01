using System.Net;
using System.Text;
using Jarvis.Core.Api;
using Jarvis.Core.Models;
using Jarvis.Core.Sse;

namespace Jarvis.Core.Tests;

public class ChatStreamClientTests
{
    private sealed class StubHandler(Func<HttpRequestMessage, HttpResponseMessage> respond) : HttpMessageHandler
    {
        public string? RequestBody { get; private set; }

        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct)
        {
            RequestBody = request.Content is null ? null : await request.Content.ReadAsStringAsync(ct);
            return respond(request);
        }
    }

    private static HttpResponseMessage Sse(string wire, int chunk = 5) => new(HttpStatusCode.OK)
    {
        Content = new StreamContent(new TrickleStream(Encoding.UTF8.GetBytes(wire), chunk)),
    };

    private static (ChatStreamClient client, StubHandler handler) Client(Func<HttpRequestMessage, HttpResponseMessage> respond)
    {
        var handler = new StubHandler(respond);
        return (new ChatStreamClient(new HttpClient(handler) { BaseAddress = new Uri("http://localhost:8000") }), handler);
    }

    [Fact]
    public async Task Tokens_then_done_reach_their_callbacks_in_order()
    {
        var (client, _) = Client(_ => Sse(
            "event: token\ndata: {\"delta\":\"Hel\"}\n\nevent: token\ndata: {\"delta\":\"lo\"}\n\n" +
            "event: done\ndata: {\"response\":\"Hello\",\"model\":\"m\",\"provider\":\"llama_cpp\",\"status\":\"ok\",\"session_id\":\"s1\"}\n\n"));
        var tokens = new List<string>();
        string? done = null;

        await client.StreamChatAsync(new StreamChatOptions
        {
            Message = "hi",
            Callbacks = new ChatStreamCallbacks
            {
                OnToken = e => tokens.Add(e.Delta),
                OnDone = e => done = e.Response + "/" + e.SessionId,
            },
        });

        Assert.Equal(new[] { "Hel", "lo" }, tokens);
        Assert.Equal("Hello/s1", done);
    }

    [Fact]
    public async Task A_confirmation_that_ends_the_stream_without_done_completes_normally()
    {
        var (client, _) = Client(_ => Sse(
            "event: confirmation_required\ndata: {\"pending_confirmations\":[{\"action_id\":\"act_abc\"," +
            "\"tool\":\"delete_file\",\"args\":{\"path\":\"a.txt\"},\"risk_tier\":\"HIGH_RISK\"}],\"session_id\":\"s1\"}\n\n"));
        SseConfirmationRequiredEvent? card = null;
        var done = false;
        var errored = false;

        await client.StreamChatAsync(new StreamChatOptions
        {
            Message = "delete a.txt",
            Callbacks = new ChatStreamCallbacks
            {
                OnConfirmationRequired = e => card = e,
                OnDone = _ => done = true,
                OnError = _ => errored = true,
            },
        });

        Assert.NotNull(card);
        var pending = Assert.Single(card!.PendingConfirmations);
        Assert.Equal("act_abc", pending.ActionId);
        Assert.Equal("delete_file", pending.Tool);
        Assert.False(done);
        Assert.False(errored);
    }

    [Fact]
    public async Task Malformed_and_unknown_events_do_not_break_the_stream()
    {
        var (client, _) = Client(_ => Sse(
            "event: token\ndata: not json at all\n\n" +
            "event: from_the_future\ndata: {\"x\":1}\n\n" +
            "event: error\ndata: plain failure\n\n" +
            "event: token\ndata: {\"delta\":\"after\"}\n\n"));
        var tokens = new List<string>();
        var errors = new List<string>();

        await client.StreamChatAsync(new StreamChatOptions
        {
            Message = "x",
            Callbacks = new ChatStreamCallbacks { OnToken = e => tokens.Add(e.Delta), OnError = e => errors.Add(e.Error) },
        });

        Assert.Equal(new[] { "not json at all", "after" }, tokens);
        Assert.Equal(new[] { "plain failure" }, errors);
    }

    [Fact]
    public async Task A_duplicated_event_is_delivered_twice_because_the_client_does_not_dedupe()
    {
        const string frame = "event: token\ndata: {\"delta\":\"a\"}\n\n";
        var (client, _) = Client(_ => Sse(frame + frame));
        var count = 0;

        await client.StreamChatAsync(new StreamChatOptions
        {
            Message = "x",
            Callbacks = new ChatStreamCallbacks { OnToken = _ => count++ },
        });

        Assert.Equal(2, count); // pins current behaviour: if dedupe is ever added, change this on purpose
    }

    [Fact]
    public async Task Hanging_up_mid_stream_returns_quietly_with_no_error_callback()
    {
        using var cts = new CancellationTokenSource();
        var (client, _) = Client(_ => Sse(
            "event: token\ndata: {\"delta\":\"a\"}\n\nevent: token\ndata: {\"delta\":\"b\"}\n\n", chunk: 1));
        var tokens = new List<string>();
        var errored = false;

        await client.StreamChatAsync(new StreamChatOptions
        {
            Message = "x",
            Callbacks = new ChatStreamCallbacks
            {
                OnToken = e => { tokens.Add(e.Delta); cts.Cancel(); },
                OnError = _ => errored = true,
            },
        }, cts.Token);

        Assert.Equal(new[] { "a" }, tokens);
        Assert.False(errored);
    }

    [Fact]
    public async Task An_http_error_calls_OnError_and_throws_ApiException()
    {
        var (client, _) = Client(_ => new HttpResponseMessage(HttpStatusCode.Unauthorized) { Content = new StringContent("nope") });
        var errors = new List<string>();

        var ex = await Assert.ThrowsAsync<ApiException>(() => client.StreamChatAsync(new StreamChatOptions
        {
            Message = "x",
            Callbacks = new ChatStreamCallbacks { OnError = e => errors.Add(e.Error) },
        }));

        Assert.Contains("401", ex.Message);
        Assert.Single(errors);
    }

    [Fact]
    public async Task The_request_body_is_snake_case_as_the_backend_requires()
    {
        var (client, handler) = Client(_ => Sse(""));

        await client.StreamChatAsync(new StreamChatOptions
        {
            Message = "hello",
            SessionId = "s9",
            ChatMode = "FREEFORM",
            Ephemeral = true,
            Callbacks = new ChatStreamCallbacks(),
        });

        Assert.NotNull(handler.RequestBody);
        Assert.Contains("\"message\":\"hello\"", handler.RequestBody);
        Assert.Contains("\"session_id\":\"s9\"", handler.RequestBody);
        Assert.Contains("\"chat_mode\":\"FREEFORM\"", handler.RequestBody);
        Assert.Contains("\"ephemeral\":true", handler.RequestBody);
        Assert.DoesNotContain("\"Message\"", handler.RequestBody);
    }
}
