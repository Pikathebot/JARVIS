using System.Text.Json;
using Jarvis.Core.Models;
using Jarvis_App.Themes;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;

namespace Jarvis_App.Views;

/// <summary>One tool call as a single line (status icon, tool, status word) that opens to
/// its arguments and result.</summary>
public sealed partial class ToolStepCard : UserControl
{
    public ToolStepCard(ToolStep step)
    {
        InitializeComponent();
        ToolName.Text = step.Tool;

        var (label, kind) = step.Status switch
        {
            ToolStatus.Running => ("Running", StatusKind.Warning),
            ToolStatus.Success => ("Done", StatusKind.Success),
            ToolStatus.Error => ("Failed", StatusKind.Error),
            _ => ("Unknown", StatusKind.Neutral),
        };
        var color = StatusStyle.ColorOf(kind);
        StatusText.Text = label;
        StatusText.Foreground = new SolidColorBrush(color);
        StatusIcon.Glyph = StatusStyle.GlyphOf(kind, inProgress: step.Status == ToolStatus.Running);
        StatusIcon.Foreground = new SolidColorBrush(color);

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

    private void Row_Click(object sender, Microsoft.UI.Xaml.RoutedEventArgs e)
    {
        var open = Detail.Visibility != Microsoft.UI.Xaml.Visibility.Visible;
        Detail.Visibility = open ? Microsoft.UI.Xaml.Visibility.Visible : Microsoft.UI.Xaml.Visibility.Collapsed;
        Chevron.Glyph = open ? "\uE70D" : "\uE76C"; // chevron down / right
    }
}
