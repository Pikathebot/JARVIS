using System.ComponentModel;
using Jarvis.Core.Models;
using Microsoft.UI.Dispatching;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media.Animation;

namespace Jarvis_App.Views;

/// <summary>One stretch of thinking as a line that opens to its text (PLAN 4.23). Follows its
/// segment while it streams: the line shows the narrator's words (or "Thinking…") and breathes,
/// then settles to "Thought for N s".</summary>
public sealed partial class ThinkingCard : UserControl
{
    private readonly ThinkingSegment _segment;
    private readonly DispatcherQueueTimer _textThrottle;
    private Storyboard? _breathing;

    public ThinkingCard(ThinkingSegment segment)
    {
        InitializeComponent();
        _segment = segment;

        // Thinking streams dozens of chunks a second; the open text is redrawn at most ~6 times a second.
        _textThrottle = DispatcherQueue.CreateTimer();
        _textThrottle.IsRepeating = false;
        _textThrottle.Interval = TimeSpan.FromMilliseconds(150);
        _textThrottle.Tick += (_, _) => DetailText.Text = _segment.Text.Trim();

        Loaded += (_, _) =>
        {
            _segment.PropertyChanged -= OnSegmentChanged;
            _segment.PropertyChanged += OnSegmentChanged;
            Refresh();
        };
        Unloaded += (_, _) =>
        {
            _segment.PropertyChanged -= OnSegmentChanged;
            _textThrottle.Stop();
            _breathing?.Stop();
        };
        SetOpen(_segment.IsOpen);
        Refresh();
    }

    private void OnSegmentChanged(object? sender, PropertyChangedEventArgs e) =>
        DispatcherQueue.TryEnqueue(() =>
        {
            if (e.PropertyName == nameof(ThinkingSegment.Text))
            {
                if (_segment.IsOpen && !_textThrottle.IsRunning) _textThrottle.Start();
                return;
            }
            Refresh();
        });

    private void Refresh()
    {
        Label.Text = _segment.IsActive
            ? (string.IsNullOrWhiteSpace(_segment.Status) ? "Thinking…" : _segment.Status + "…")
            : _segment.Seconds is double s ? $"Thought for {FormatSeconds(s)}" : "Thought process";
        if (_segment.IsOpen) DetailText.Text = _segment.Text.Trim();
        SetBreathing(_segment.IsActive);
    }

    private static string FormatSeconds(double seconds) =>
        seconds < 1 ? "a moment" : seconds < 60 ? $"{Math.Round(seconds)} s" : $"{(int)(seconds / 60)} min {Math.Round(seconds % 60)} s";

    /// <summary>A slow fade in and out of the line while it is live -- the only motion it has.</summary>
    private void SetBreathing(bool on)
    {
        if (!on)
        {
            _breathing?.Stop();
            _breathing = null;
            Label.Opacity = 1;
            return;
        }
        if (_breathing is not null) return;
        var fade = new DoubleAnimation
        {
            From = 1.0,
            To = 0.45,
            Duration = new Duration(TimeSpan.FromMilliseconds(900)),
            AutoReverse = true,
            RepeatBehavior = RepeatBehavior.Forever,
            EasingFunction = new SineEase { EasingMode = EasingMode.EaseInOut },
        };
        Storyboard.SetTarget(fade, Label);
        Storyboard.SetTargetProperty(fade, "Opacity");
        _breathing = new Storyboard();
        _breathing.Children.Add(fade);
        _breathing.Begin();
    }

    private void SetOpen(bool open)
    {
        _segment.IsOpen = open;
        Detail.Visibility = open ? Visibility.Visible : Visibility.Collapsed;
        Chevron.Glyph = open ? "" : ""; // chevron down / right
        if (open) DetailText.Text = _segment.Text.Trim();
    }

    private void Row_Click(object sender, RoutedEventArgs e) => SetOpen(!_segment.IsOpen);
}
