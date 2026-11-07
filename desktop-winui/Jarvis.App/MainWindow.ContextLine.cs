using Jarvis.Core.Models;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Shapes;

namespace Jarvis_App;

/// <summary>
/// The context line in the toolbar row and its popover (design system: ContextLine; PLAN 4.18).
/// They replace the right panel's Context tab: how full the model's context was on the last turn,
/// the four token tiers and the passages retrieved for it. Same data as before
/// (<see cref="ViewModels.ChatViewModel.LatestRetrieval"/>), new place.
/// </summary>
public sealed partial class MainWindow
{
    /// <summary>Meter colour breaks, in percent of the input budget (compaction runs at 80).</summary>
    private const double ContextHighPercent = 80;
    private const double ContextCriticalPercent = 95;
    private const double ContextLineMeterWidth = 48;
    private const int ContextPassagesShown = 5;

    private static string Short(int tokens) => tokens >= 1000 ? $"{tokens / 1000.0:0.#}k" : tokens.ToString();

    private static string MeterBrush(double percent) =>
        percent >= ContextCriticalPercent ? "MeterCritical" : percent >= ContextHighPercent ? "MeterHigh" : "MeterNormal";

    /// <summary>Refreshes the line (and the popover, when open) from the last turn's budget.</summary>
    private void UpdateContextLine()
    {
        var budget = ChatViewModel.LatestRetrieval?.BudgetReport;
        if (budget is null || budget.AvailableInputBudget <= 0)
        {
            ContextLineText.Text = "Context · no turn yet";
            ContextLineTrack.Visibility = Visibility.Collapsed;
        }
        else
        {
            var percent = 100.0 * budget.TotalInputTokensUsed / budget.AvailableInputBudget;
            ContextLineText.Text = $"Context {percent:0}% · {Short(budget.TotalInputTokensUsed)} of {Short(budget.AvailableInputBudget)}";
            ContextLineFill.Width = ContextLineMeterWidth * Math.Clamp(percent / 100, 0, 1);
            ContextLineFill.Fill = Themes.JarvisTheme.Brush(MeterBrush(percent));
            ContextLineTrack.Visibility = Visibility.Visible;
        }
        if (ContextPopover.Visibility == Visibility.Visible) BuildContextPopover();
    }

    private void ContextLine_Click(object sender, RoutedEventArgs e) =>
        SetContextPopoverOpen(ContextPopover.Visibility != Visibility.Visible);

    /// <summary>Opens the popover under the line, left edges aligned. Like the status popover, the
    /// transcript's text fades while it is open, since glass can't frost XAML.</summary>
    private void SetContextPopoverOpen(bool open)
    {
        if (open == (ContextPopover.Visibility == Visibility.Visible)) return;
        if (open)
        {
            SetWorkspaceDropdownOpen(false);
            SetStatusPopoverOpen(false);
            CloseSidebarOverlay(); // a compact (floating) sidebar would overlap the popover
            var line = ContextLineButton.TransformToVisual(RootGrid).TransformBounds(
                new Windows.Foundation.Rect(0, 0, ContextLineButton.ActualWidth, ContextLineButton.ActualHeight));
            ContextPopover.Margin = new Thickness(Math.Max(8, line.Left), line.Bottom + 8, 0, 0);
            BuildContextPopover();
        }
        ContextPopover.Visibility = open ? Visibility.Visible : Visibility.Collapsed;
        _ = Motion(SurfaceContent, affectsGlass: false).AnimateTo(0, 0, open ? PopoverBackdropOpacity : 1.0, Backdrop);
    }

    private static Grid Meter(double percent, double height)
    {
        var track = new Grid { Height = height, CornerRadius = new CornerRadius(height / 2), Background = Themes.JarvisTheme.Brush("MeterTrack") };
        var fill = new Rectangle
        {
            RadiusX = height / 2,
            RadiusY = height / 2,
            Fill = Themes.JarvisTheme.Brush(MeterBrush(percent)),
            HorizontalAlignment = HorizontalAlignment.Left,
        };
        // Width follows the track: a fraction of whatever width the popover gives it.
        track.SizeChanged += (_, args) => fill.Width = args.NewSize.Width * Math.Clamp(percent / 100, 0, 1);
        track.Children.Add(fill);
        return track;
    }

    private static Grid LabelValue(string label, string value, string labelStyle = "BodyText")
    {
        var row = new Grid();
        row.Children.Add(new TextBlock { Text = label, Style = Themes.JarvisTheme.Style(labelStyle), TextTrimming = TextTrimming.CharacterEllipsis, Margin = new Thickness(0, 0, 64, 0) });
        row.Children.Add(new TextBlock { Text = value, Style = Themes.JarvisTheme.Style("DataText"), HorizontalAlignment = HorizontalAlignment.Right });
        return row;
    }

    private void BuildContextPopover()
    {
        var body = ContextPopoverBody;
        body.Children.Clear();
        body.Children.Add(new TextBlock { Text = "Context", Style = Themes.JarvisTheme.Style("HeadlineText") });

        var retrieval = ChatViewModel.LatestRetrieval;
        var budget = retrieval?.BudgetReport;
        if (retrieval is null || budget is null || budget.AvailableInputBudget <= 0)
        {
            body.Children.Add(new TextBlock { Text = "No turn yet. Send a message and this shows what the model could see.", Style = Themes.JarvisTheme.Style("SecondaryFootnoteText"), TextWrapping = TextWrapping.Wrap });
            return;
        }

        var window = budget.AvailableInputBudget;
        var percent = 100.0 * budget.TotalInputTokensUsed / window;
        body.Children.Add(new TextBlock { Text = $"{budget.TotalInputTokensUsed:N0} of {window:N0} tokens", Style = Themes.JarvisTheme.Style("SecondaryFootnoteText") });
        body.Children.Add(Meter(percent, 6));

        var tiers = new (string Name, int Tokens)[]
        {
            ("Instructions and tools", budget.Tier1SystemTokens),
            ("Your message and files", budget.Tier2UserTokens),
            ("Retrieved passages", budget.Tier3RagTokens),
            ("Conversation so far", budget.Tier4HistoryTokens),
        };
        foreach (var (name, tokens) in tiers)
        {
            var tier = new StackPanel { Spacing = 3, Margin = new Thickness(0, 4, 0, 0) };
            tier.Children.Add(LabelValue(name, tokens.ToString("N0")));
            tier.Children.Add(Meter(100.0 * tokens / window, 4));
            body.Children.Add(tier);
        }

        if (retrieval.ChunksUsed.Count > 0)
        {
            body.Children.Add(new TextBlock { Text = "Retrieved passages", Style = Themes.JarvisTheme.Style("SectionHeaderText"), Margin = new Thickness(0, 12, 0, 0) });
            foreach (var chunk in retrieval.ChunksUsed.Take(ContextPassagesShown))
            {
                var lines = chunk.StartLine is int start && chunk.EndLine is int end ? $"L{start}-{end}" : "";
                body.Children.Add(LabelValue(chunk.FileName ?? System.IO.Path.GetFileName(chunk.FilePath ?? "") ?? "", lines));
            }
            if (retrieval.ChunksUsed.Count > ContextPassagesShown)
            {
                body.Children.Add(new TextBlock { Text = $"and {retrieval.ChunksUsed.Count - ContextPassagesShown} more", Style = Themes.JarvisTheme.Style("SecondaryFootnoteText") });
            }
        }
        if (retrieval.ChunksDropped.Count > 0)
        {
            body.Children.Add(new TextBlock
            {
                Text = $"{retrieval.ChunksDropped.Count} dropped (over budget)",
                Style = Themes.JarvisTheme.Style("FootnoteText"),
                Foreground = Themes.JarvisTheme.Brush("StatusWarning"),
            });
        }

        body.Children.Add(new TextBlock
        {
            Text = "What the model could read on the last turn. The chat is compacted on its own at 80%, or type /compact.",
            Style = Themes.JarvisTheme.Style("SecondaryFootnoteText"),
            TextWrapping = TextWrapping.Wrap,
            Margin = new Thickness(0, 8, 0, 0),
        });
    }
}
