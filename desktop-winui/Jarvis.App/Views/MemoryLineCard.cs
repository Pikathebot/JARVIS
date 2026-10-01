using Jarvis.Core.Models;
using Jarvis_App.Themes;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;

namespace Jarvis_App.Views;

/// <summary>
/// "Remembered: ..." / "Forgot: ..." as a line of its own in the chat, with Undo (tool review,
/// 2026-09-30): a memory change used to hide in a collapsed tool row, so a wrong forget went
/// unnoticed. Undo on a remembered fact moves it to the bin; on a forgotten one it restores it
/// from the bin -- both through the backend, and both restorable again from there.
/// </summary>
public sealed partial class MemoryLineCard : UserControl
{
    private readonly ToolStep _step;
    private readonly MemoryLine _line;
    private readonly Func<ToolStep, MemoryLine, Task<string?>>? _undo;
    private readonly TextBlock _text;
    private readonly HyperlinkButton _undoButton;

    public MemoryLineCard(ToolStep step, MemoryLine line, Func<ToolStep, MemoryLine, Task<string?>>? undo)
    {
        _step = step;
        _line = line;
        _undo = undo;

        var row = new Grid { ColumnSpacing = 8, Margin = new Thickness(0, 0, 0, 2), MaxWidth = 560, HorizontalAlignment = HorizontalAlignment.Left };
        row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });

        var icon = new FontIcon
        {
            Style = JarvisTheme.Style("JarvisIcon"),
            Glyph = line.IsForget ? "" : "", // delete / pin
            FontSize = 13,
            Foreground = JarvisTheme.Brush("LabelSecondary"),
            VerticalAlignment = VerticalAlignment.Center,
        };
        _text = new TextBlock
        {
            Style = JarvisTheme.Style("FootnoteText"),
            Foreground = JarvisTheme.Brush("LabelSecondary"),
            TextWrapping = TextWrapping.Wrap,
            IsTextSelectionEnabled = true,
            VerticalAlignment = VerticalAlignment.Center,
        };
        Grid.SetColumn(_text, 1);
        _undoButton = new HyperlinkButton { Content = "Undo", Padding = new Thickness(6, 2, 6, 2), VerticalAlignment = VerticalAlignment.Center };
        _undoButton.Click += Undo_Click;
        Grid.SetColumn(_undoButton, 2);

        row.Children.Add(icon);
        row.Children.Add(_text);
        row.Children.Add(_undoButton);
        Content = row;
        Show();
    }

    private void Show()
    {
        _text.Text = _step.UndoneText ?? $"{_line.Verb}: {_line.Content}";
        _undoButton.Visibility = _step.UndoneText is null && _line.ShortId is not null && _undo is not null
            ? Visibility.Visible : Visibility.Collapsed;
    }

    private async void Undo_Click(object sender, RoutedEventArgs e)
    {
        if (_undo is null) return;
        _undoButton.IsEnabled = false;
        var said = await _undo(_step, _line);
        _undoButton.IsEnabled = true;
        if (said is null) return;
        _step.UndoneText = said;
        Show();
    }
}
