using Jarvis_Glass;
using Windows.UI;

namespace Jarvis_App.Themes;

/// <summary>The design system's status kinds.</summary>
public enum StatusKind
{
    Success,
    Warning,
    Error,
    Info,
    Neutral,
}

/// <summary>
/// Status is always an icon, a word and a colour (design system: StatusBadge). One mapping
/// from kind to colour token and Segoe Fluent glyph, so badges, the governor pill, awareness
/// cards and the activity list agree. Colours come from the current appearance.
/// </summary>
public static class StatusStyle
{
    public static Color ColorOf(StatusKind kind)
    {
        var c = JarvisPalette.Current;
        return kind switch
        {
            StatusKind.Success => c.StatusSuccess,
            StatusKind.Warning => c.StatusWarning,
            StatusKind.Error => c.StatusError,
            StatusKind.Info => c.StatusInfo,
            _ => c.StatusNeutral,
        };
    }

    /// <summary>CheckMark, Warning, ErrorBadge, Info, and Sync for work in progress.</summary>
    public static string GlyphOf(StatusKind kind, bool inProgress = false) => kind switch
    {
        StatusKind.Success => "",
        StatusKind.Warning => inProgress ? "" : "",
        StatusKind.Error => "",
        _ => "",
    };

    /// <summary>A tool step's or activity entry's status string ("success", "error", "running"...).</summary>
    public static StatusKind FromStepStatus(string? status) => status switch
    {
        "success" or "completed" or "ok" => StatusKind.Success,
        "error" or "failed" => StatusKind.Error,
        null or "" or "unknown" => StatusKind.Neutral,
        _ => StatusKind.Warning,
    };
}
