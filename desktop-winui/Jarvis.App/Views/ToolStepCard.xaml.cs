using System.Text.Json;
using Jarvis.Core.Models;
using Jarvis_App.Themes;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;

namespace Jarvis_App.Views;

/// <summary>Native port of ToolStepCard.tsx: collapsible inline tool call card with a status badge.</summary>
public sealed partial class ToolStepCard : UserControl
{
    public ToolStepCard(ToolStep step)
    {
        InitializeComponent();
        ToolName.Text = step.Tool;

        var (label, kind) = step.Status switch
        {
            ToolStatus.Running => ("Running", StatusKind.Warning),
            ToolStatus.Success => ("Completed", StatusKind.Success),
            ToolStatus.Error => ("Failed", StatusKind.Error),
            _ => ("Unknown", StatusKind.Neutral),
        };
        var color = StatusStyle.ColorOf(kind);
        StatusText.Text = label;
        StatusText.Foreground = new SolidColorBrush(color);
        StatusIcon.Glyph = StatusStyle.GlyphOf(kind, inProgress: step.Status == ToolStatus.Running);
        StatusIcon.Foreground = new SolidColorBrush(color);
        StatusBadge.Background = JarvisTheme.Tinted(color, 0.14);

        try
        {
            ArgsText.Text = step.Args.Count > 0
                ? JsonSerializer.Serialize(step.Args, new JsonSerializerOptions { WriteIndented = true })
                : step.RawArgs ?? "";
        }
        catch
        {
            ArgsText.Text = step.RawArgs ?? "";
        }

        if (step.Result is null)
        {
            ResultLabel.Visibility = Microsoft.UI.Xaml.Visibility.Collapsed;
            ResultText.Visibility = Microsoft.UI.Xaml.Visibility.Collapsed;
        }
        else
        {
            try
            {
                ResultText.Text = JsonSerializer.Serialize(step.Result, new JsonSerializerOptions { WriteIndented = true });
            }
            catch
            {
                ResultText.Text = step.Result.ToString() ?? "";
            }
        }
    }
}
