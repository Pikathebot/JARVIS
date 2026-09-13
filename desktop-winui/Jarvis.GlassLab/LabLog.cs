namespace Jarvis_GlassLab;

/// <summary>Tiny append-only log shared by everything in the lab (App sets the path at launch).</summary>
internal static class LabLog
{
    public static string? Path { get; set; }

    public static void Write(string message)
    {
        if (Path is null) return;
        try { File.AppendAllText(Path, $"[{DateTimeOffset.Now:O}] {message}\n"); } catch { }
    }
}
