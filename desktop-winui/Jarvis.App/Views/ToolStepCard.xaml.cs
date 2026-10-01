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
        ToolName.Text = ToolStepText.RowLabel(step);
        if (ToolName.Text != step.Tool) ToolTipService.SetToolTip(ToolName, ToolName.Text);

        _shotPath = ToolStepText.ScreenshotPath(step);
        if (_shotPath is not null)
        {
            try
            {
                ShotImage.Source = new Microsoft.UI.Xaml.Media.Imaging.BitmapImage(new Uri(_shotPath)) { DecodePixelHeight = 360 };
                ShotButton.Visibility = Microsoft.UI.Xaml.Visibility.Visible;
                ToolTipService.SetToolTip(ShotButton, "Open the screenshot");
            }
            catch
            {
                _shotPath = null; // unreadable: the row alone, as before
            }
        }

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

    private readonly string? _shotPath;

    private void Shot_Click(object sender, Microsoft.UI.Xaml.RoutedEventArgs e)
    {
        if (_shotPath is null) return;
        try
        {
            System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo(_shotPath) { UseShellExecute = true });
        }
        catch
        {
            // no viewer registered for .png: nothing to do
        }
    }

    private void Row_Click(object sender, Microsoft.UI.Xaml.RoutedEventArgs e)
    {
        var open = Detail.Visibility != Microsoft.UI.Xaml.Visibility.Visible;
        Detail.Visibility = open ? Microsoft.UI.Xaml.Visibility.Visible : Microsoft.UI.Xaml.Visibility.Collapsed;
        Chevron.Glyph = open ? "\uE70D" : "\uE76C"; // chevron down / right
    }
}
