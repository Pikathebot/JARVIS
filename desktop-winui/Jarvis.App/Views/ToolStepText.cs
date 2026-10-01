using System.Text.Json;
using System.Text.RegularExpressions;
using Jarvis.Core.Models;

namespace Jarvis_App.Views;

/// <summary>A remember/forget result as the chat line shows it.</summary>
public sealed record MemoryLine(string Verb, string Content, string? ShortId, bool IsForget);

/// <summary>Reading a tool step's arguments and result for the chat (both arrive as JSON
/// elements or strings, live and from history alike).</summary>
public static partial class ToolStepText
{
    public static string Arg(ToolStep step, string key)
    {
        if (!step.Args.TryGetValue(key, out var value) || value is null) return "";
        return value switch
        {
            JsonElement { ValueKind: JsonValueKind.String } je => je.GetString() ?? "",
            JsonElement je => je.ToString(),
            _ => value.ToString() ?? "",
        };
    }

    public static string Result(ToolStep step) => step.Result switch
    {
        null => "",
        string s => s,
        JsonElement { ValueKind: JsonValueKind.String } je => je.GetString() ?? "",
        JsonElement je => je.ToString(),
        var other => other.ToString() ?? "",
    };

    /// <summary>The collapsed row's label: what the step did, not just the tool's name, for the
    /// tools whose one argument says it ("Searched: weather in Chennai").</summary>
    public static string RowLabel(ToolStep step)
    {
        var running = step.Status == ToolStatus.Running;
        string Labelled(string done, string doing, string key)
        {
            var value = Arg(step, key);
            return value.Length > 0 ? $"{(running ? doing : done)}: {value}" : step.Tool;
        }
        return step.Tool switch
        {
            "web_search" => Labelled("Searched", "Searching", "query"),
            "fetch_url" => Labelled("Read", "Reading", "url"),
            "look_at_screen" => Arg(step, "window") is { Length: > 0 } w
                ? $"{(running ? "Looking at" : "Looked at")}: {w}"
                : running ? "Looking at the screen" : "Looked at the screen",
            _ => step.Tool,
        };
    }

    /// <summary>The image look_at_screen saved, if the step has one and the file is still there.</summary>
    public static string? ScreenshotPath(ToolStep step)
    {
        if (step.Tool != "look_at_screen") return null;
        var m = ScreenshotMarker().Match(Result(step));
        return m.Success && File.Exists(m.Groups[1].Value) ? m.Groups[1].Value : null;
    }

    /// <summary>A remember/forget that changed something, as "Remembered: ..." / "Forgot: ...";
    /// null for everything else (refusals, duplicates and errors stay ordinary tool rows).</summary>
    public static MemoryLine? Memory(ToolStep step)
    {
        if (step.Status != ToolStatus.Success) return null;
        var result = Result(step);
        if (step.Tool == "remember")
        {
            var content = Arg(step, "content");
            var m = MemoryId().Match(result);
            if (result.StartsWith("Remembered [") && m.Success)
                return new MemoryLine("Remembered", content, m.Groups[1].Value, IsForget: false);
            if (result.StartsWith("Updated memory ["))
                return new MemoryLine("Updated memory", content, null, IsForget: false);
        }
        else if (step.Tool == "forget" && result.StartsWith("Forgot ["))
        {
            var m = MemoryId().Match(result);
            var colon = result.IndexOf("]: ", StringComparison.Ordinal);
            if (m.Success && colon > 0)
                return new MemoryLine("Forgot", result[(colon + 3)..], m.Groups[1].Value, IsForget: true);
        }
        return null;
    }

    [GeneratedRegex(@"\[screenshot:([^\]]+)\]")]
    private static partial Regex ScreenshotMarker();

    [GeneratedRegex(@"\[([0-9a-f]{4,})\]")]
    private static partial Regex MemoryId();
}
