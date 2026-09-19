using System.Runtime.CompilerServices;

namespace Jarvis.Core.Sse;

/// <summary>
/// Frames a raw SSE byte stream into (event, data) packets. Equivalent to the hand-rolled parser in
/// the retired Next.js client's src/lib/sse-client.ts, but built on StreamReader.ReadLineAsync instead of manual
/// double-newline splitting — line framing already absorbs the CRLF/LF ambiguity that file handled by hand.
/// A blank line ends a packet (the SSE spec's frame boundary); multiple "data:" lines are joined with "\n".
/// ":"-prefixed lines are comments (keepalives) and are dropped without producing a frame.
/// </summary>
public static class SseReader
{
    public static async IAsyncEnumerable<SseFrame> ReadFramesAsync(
        Stream stream,
        [EnumeratorCancellation] CancellationToken cancellationToken = default)
    {
        using var reader = new StreamReader(stream, System.Text.Encoding.UTF8);

        string eventType = "message";
        List<string>? dataLines = null;

        while (true)
        {
            cancellationToken.ThrowIfCancellationRequested();

            string? line;
            try
            {
                line = await reader.ReadLineAsync(cancellationToken).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                yield break;
            }

            if (line is null)
            {
                // Stream ended — flush a trailing packet that had no closing blank line.
                if (dataLines is { Count: > 0 })
                {
                    yield return new SseFrame(eventType, string.Join('\n', dataLines));
                }
                yield break;
            }

            if (line.Length == 0)
            {
                if (dataLines is { Count: > 0 })
                {
                    yield return new SseFrame(eventType, string.Join('\n', dataLines));
                }
                eventType = "message";
                dataLines = null;
                continue;
            }

            if (line.StartsWith(':'))
            {
                continue; // comment / keepalive
            }

            if (line.StartsWith("event:", StringComparison.Ordinal))
            {
                eventType = line[6..].Trim();
            }
            else if (line.StartsWith("data:", StringComparison.Ordinal))
            {
                dataLines ??= new List<string>();
                var value = line[5..];
                if (value.StartsWith(' '))
                {
                    value = value[1..];
                }
                dataLines.Add(value);
            }
            // other field types (id:, retry:) are not used by this backend — ignored.
        }
    }
}
