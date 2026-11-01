using System.Text;
using Jarvis.Core.Sse;

namespace Jarvis.Core.Tests;

public class SseReaderTests
{
    private static async Task<List<SseFrame>> ReadAsync(Stream stream)
    {
        var frames = new List<SseFrame>();
        await foreach (var frame in SseReader.ReadFramesAsync(stream)) frames.Add(frame);
        return frames;
    }

    private static MemoryStream Utf8(string text) => new(Encoding.UTF8.GetBytes(text));

    private const string Wire =
        "event: token\ndata: {\"delta\":\"héllo ☃ \U0001F600\"}\n\n" +
        ": keepalive\n\n" +
        "event: done\ndata: {\"response\":\"ok\"}\n\n";

    [Theory]
    [InlineData(1)]
    [InlineData(2)]
    [InlineData(3)]
    [InlineData(7)]
    [InlineData(4096)]
    public async Task Frames_are_the_same_wherever_the_bytes_are_cut(int chunk)
    {
        var frames = await ReadAsync(new TrickleStream(Encoding.UTF8.GetBytes(Wire), chunk));

        Assert.Equal(2, frames.Count);
        Assert.Equal("token", frames[0].EventType);
        Assert.Contains("héllo ☃ \U0001F600", frames[0].Data);
        Assert.Equal(new SseFrame("done", "{\"response\":\"ok\"}"), frames[1]);
    }

    [Fact]
    public async Task Crlf_line_endings_frame_like_lf()
    {
        var bytes = Encoding.UTF8.GetBytes("event: token\r\ndata: a\r\n\r\nevent: done\r\ndata: b\r\n\r\n");
        var frames = await ReadAsync(new TrickleStream(bytes, 3));
        Assert.Equal(new[] { new SseFrame("token", "a"), new SseFrame("done", "b") }, frames);
    }

    [Fact]
    public async Task Multiple_data_lines_join_with_a_newline_and_event_defaults_to_message()
    {
        var frames = await ReadAsync(Utf8("data: one\ndata: two\n\n"));
        Assert.Equal(new SseFrame("message", "one\ntwo"), Assert.Single(frames));
    }

    [Fact]
    public async Task A_trailing_packet_without_its_blank_line_is_still_delivered()
    {
        var frames = await ReadAsync(Utf8("event: done\ndata: x"));
        Assert.Equal(new SseFrame("done", "x"), Assert.Single(frames));
    }

    [Fact]
    public async Task Event_type_does_not_leak_into_the_next_packet()
    {
        var frames = await ReadAsync(Utf8("event: token\ndata: a\n\ndata: b\n\n"));
        Assert.Equal("token", frames[0].EventType);
        Assert.Equal("message", frames[1].EventType);
    }

    [Fact]
    public async Task Comments_unknown_fields_and_event_only_packets_produce_no_frame()
    {
        Assert.Empty(await ReadAsync(Utf8(": ping\n\nid: 7\nretry: 100\n\nevent: token\n\n")));
    }

    [Fact]
    public async Task Cancelling_between_frames_surfaces_as_OperationCanceledException()
    {
        // The caller (ChatStreamClient) owns swallowing it; see ChatStreamClientTests.
        using var cts = new CancellationTokenSource();
        var frames = new List<SseFrame>();
        await Assert.ThrowsAnyAsync<OperationCanceledException>(async () =>
        {
            await foreach (var frame in SseReader.ReadFramesAsync(
                               new TrickleStream(Encoding.UTF8.GetBytes(Wire), 1), cts.Token))
            {
                frames.Add(frame);
                cts.Cancel();
            }
        });
        Assert.Single(frames);
    }
}
