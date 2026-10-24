using System.ComponentModel;
using System.Text.Json;
using Jarvis.Core.Models;
using Jarvis_App.Rendering;
using Microsoft.UI.Dispatching;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using Windows.UI;

namespace Jarvis_App.Views;

/// <summary>
/// Native port of MessageBubble.tsx. Renders whichever of the three variants (system/user/
/// assistant) the bound ChatMessage.Role calls for, re-rendering markdown content as it
/// changes via MarkdownRenderer, coalesced while streaming (see _renderThrottle). Relies on classic
/// Binding/DataContext propagation from the hosting ItemsRepeater's DataTemplate — see the note on
/// MainWindow.xaml's MessageList template.
/// </summary>
public sealed partial class MessageBubbleControl : UserControl
{
    public event Action<PendingConfirmation>? ApproveRequested;
    public event Action<PendingConfirmation>? DenyRequested;

    /// <summary>A vote on the reply's second take: original / alternative / tie / dismiss.</summary>
    public event Action<ChatMessage, string>? CompareVoteRequested;

    /// <summary>The Undo on a "Remembered/Forgot" line; set by the window that hosts the bubble.
    /// Returns what the line says afterwards, or null when nothing changed.</summary>
    public Func<ToolStep, MemoryLine, Task<string?>>? UndoMemory { get; set; }

    private ChatMessage? _message;

    /// <summary>
    /// Coalesces re-renders while a message is streaming.
    ///
    /// Rendering rebuilds the whole element tree from the whole message, so it costs O(content
    /// length); doing that once per token makes a single answer O(n^2) in XAML element
    /// construction -- which is not merely slow, it stops the UI thread pumping messages, and
    /// Windows terminates the window as hung ("Application Hang" in the event log). Measured: a
    /// single "hi" streams 2219 tokens / 8612 chars, so the old path re-parsed 9.7M characters
    /// and rebuilt the tree 26 times a second. Answer length, not question length, is what
    /// decides whether the window survives.
    /// </summary>
    private readonly DispatcherQueueTimer _renderThrottle;

    /// <summary>The single TextBlock reused for the whole of a streaming answer.</summary>
    private TextBlock? _streamingText;

    /// <summary>
    /// Everything the last render was built from. Rendering replaces ContentHost.Content, which
    /// invalidates layout, which makes the ListView re-measure and re-assign DataContext on this
    /// container, which lands back here -- a feedback loop that spins the UI thread at 100%
    /// indefinitely, long after the message has finished streaming. Re-rendering only when an
    /// input actually changed is what breaks it.
    /// </summary>
    private (string Content, bool Streaming, int Trace, int Confirmations, string? Model, string? Compare, bool Voted)? _rendered;

    /// <summary>The trace the steps were last built from: the answer streaming re-renders the
    /// bubble ~10 times a second, and rebuilding the steps each time closed any the user opened.</summary>
    private (ChatMessage Message, int Version, int Steps)? _builtTrace;

    public MessageBubbleControl()
    {
        InitializeComponent();

        _renderThrottle = DispatcherQueue.CreateTimer();
        _renderThrottle.IsRepeating = false;
        _renderThrottle.Tick += (_, _) => Render();

        DataContextChanged += (_, _) => Bind(DataContext as ChatMessage);
        // The ListView recycles these controls; a timer left running would render into a
        // container that has since been handed to a different message.
        Unloaded += (_, _) =>
        {
            _renderThrottle.Stop();
            Themes.JarvisTheme.Applied -= Render;
        };
        // Markdown and code runs take their brushes when built; rebuild on an appearance change.
        Loaded += (_, _) =>
        {
            Themes.JarvisTheme.Applied -= Render;
            Themes.JarvisTheme.Applied += Render;
        };
    }

    private void Bind(ChatMessage? message)
    {
        // The ListView re-sets DataContext during its own layout pass. Re-binding to the message
        // we are already showing would tear down and rebuild identical content for nothing, and
        // is the other half of the render loop described on _rendered.
        if (ReferenceEquals(_message, message)) return;

        if (_message is not null)
        {
            _message.PropertyChanged -= OnMessagePropertyChanged;
        }
        _message = message;
        if (_message is not null)
        {
            _message.PropertyChanged += OnMessagePropertyChanged;
        }

        // The ListView hands these controls to a different message; nothing built for the
        // previous one may be reused.
        _streamingText = null;
        _rendered = null;
        _builtTrace = null;
        RenderNow();
    }

    private void OnMessagePropertyChanged(object? sender, PropertyChangedEventArgs e)
    {
        switch (e.PropertyName)
        {
            // Streaming starting or finishing is a state the user must see immediately: the
            // final token's text, the caret going away, the tool steps and footer appearing.
            case nameof(ChatMessage.IsStreaming):
            // A confirmation answered by typing or voice must take its card away at once.
            case nameof(ChatMessage.PendingConfirmations):
            // A second take arriving (or voted away) shows or hides the compare card.
            case nameof(ChatMessage.CompareAlternative):
            case nameof(ChatMessage.CompareVoted):
            // A thread marker gets its title once the episode's record is written.
            case nameof(ChatMessage.MarkText):
                DispatcherQueue.TryEnqueue(RenderNow);
                break;

            // A tool step or a stretch of thinking began or finished.
            case nameof(ChatMessage.TraceVersion):
            case nameof(ChatMessage.Content):
                DispatcherQueue.TryEnqueue(
                    _message?.IsStreaming == true ? ScheduleRender : RenderNow);
                break;
        }
    }

    private void RenderNow()
    {
        _renderThrottle.Stop();
        Render();
    }

    /// <summary>
    /// Renders on a trailing edge, at most once per interval, so a burst of tokens costs one
    /// render rather than one each.
    /// </summary>
    private void ScheduleRender()
    {
        if (_renderThrottle.IsRunning) return;

        // A fixed interval only divides the quadratic cost by a constant -- a long answer would
        // still creep back towards a hang. Stretching the interval with the message keeps the
        // work per second roughly flat instead: ~10 fps for a short reply, ~2 fps once the answer
        // is long enough that a redraw is expensive and individual tokens no longer stand out.
        var length = _message?.Content.Length ?? 0;
        _renderThrottle.Interval = TimeSpan.FromMilliseconds(Math.Clamp(length / 20.0, 100, 500));
        _renderThrottle.Start();
    }

    private void Render()
    {
        var message = _message;
        if (message is null) return;

        var signature = (message.Mark == ThreadMark.None ? message.Content : message.MarkText, message.IsStreaming,
                         message.TraceVersion + message.ToolSteps.Count, message.PendingConfirmations.Count, message.Model,
                         message.CompareAlternative, message.CompareVoted);
        if (_rendered == signature) return;
        _rendered = signature;

        SystemPill.Visibility = Visibility.Collapsed;
        UserRow.Visibility = Visibility.Collapsed;
        AssistantRow.Visibility = Visibility.Collapsed;
        DayDivider.Visibility = Visibility.Collapsed;
        EpisodeMarker.Visibility = Visibility.Collapsed;
        DigestCard.Visibility = Visibility.Collapsed;
        CompactedCard.Visibility = Visibility.Collapsed;

        if (message.Mark != ThreadMark.None)
        {
            RenderMark(message);
            return;
        }

        switch (message.Role)
        {
            case MessageRole.System:
                SystemPill.Visibility = Visibility.Visible;
                SystemText.Text = message.Content;
                break;

            case MessageRole.User:
                UserRow.Visibility = Visibility.Visible;
                UserText.Text = message.Content;
                RenderUserImages(message);
                break;

            default:
                AssistantRow.Visibility = Visibility.Visible;
                RenderAssistant(message);
                break;
        }
    }

    /// <summary>The pictures sent with a user message, above its text. Rebuilt on every render,
    /// since a recycled bubble may have held another message's images.</summary>
    private void RenderUserImages(ChatMessage message)
    {
        UserImages.Children.Clear();
        UserImages.Visibility = Visibility.Collapsed;
        foreach (var path in message.ImagePaths)
        {
            try
            {
                var bitmap = new Microsoft.UI.Xaml.Media.Imaging.BitmapImage(new Uri(path)) { DecodePixelWidth = 480 };
                UserImages.Children.Add(new Border
                {
                    CornerRadius = (CornerRadius)Application.Current.Resources["RowCornerRadius"],
                    MaxWidth = 240,
                    MaxHeight = 240,
                    HorizontalAlignment = HorizontalAlignment.Left,
                    Child = new Image { Source = bitmap, Stretch = Microsoft.UI.Xaml.Media.Stretch.Uniform },
                });
            }
            catch
            {
                // a file that has gone since it was sent: the text still says what was attached
            }
        }
        if (UserImages.Children.Count > 0) UserImages.Visibility = Visibility.Visible;
    }

    /// <summary>A Freeform thread mark (PLAN 4.18): a day divider, an episode marker or a
    /// digest card, which starts closed.</summary>
    private void RenderMark(ChatMessage message)
    {
        switch (message.Mark)
        {
            case ThreadMark.Day:
                DayDivider.Visibility = Visibility.Visible;
                DayText.Text = message.MarkText;
                break;
            case ThreadMark.Episode:
                EpisodeMarker.Visibility = Visibility.Visible;
                EpisodeMarker.Text = message.MarkText;
                break;
            case ThreadMark.Digest:
                DigestCard.Visibility = Visibility.Visible;
                DigestTitle.Text = message.MarkText;
                DigestDetail.Text = message.MarkDetail;
                SetDigestOpen(false);
                break;
            case ThreadMark.Compacted:
                CompactedCard.Visibility = Visibility.Visible;
                CompactedTitle.Text = message.MarkText;
                CompactedHost.Content = MarkdownRenderer.Render(message.MarkDetail, false, false);
                SetCompactedOpen(false);
                break;
        }
    }

    private void SetCompactedOpen(bool open)
    {
        CompactedDetail.Visibility = open ? Visibility.Visible : Visibility.Collapsed;
        CompactedChevron.Glyph = open ? "" : ""; // chevron down / right
    }

    private void CompactedToggle_Click(object sender, RoutedEventArgs e) =>
        SetCompactedOpen(CompactedDetail.Visibility != Visibility.Visible);

    private void SetDigestOpen(bool open)
    {
        DigestDetail.Visibility = open ? Visibility.Visible : Visibility.Collapsed;
        DigestChevron.Glyph = open ? "" : "";
    }

    private void DigestToggle_Click(object sender, RoutedEventArgs e) =>
        SetDigestOpen(DigestDetail.Visibility != Visibility.Visible);

    /// <summary>Right-click / long-press "Copy message" on either bubble: the whole message as
    /// plain text, for when drag-selecting inside a bubble is more fiddly than wanted.</summary>
    private void CopyMessage_Click(object sender, RoutedEventArgs e)
    {
        if (DataContext is not ChatMessage message || string.IsNullOrEmpty(message.Content)) return;
        var package = new Windows.ApplicationModel.DataTransfer.DataPackage();
        package.SetText(message.Content);
        Windows.ApplicationModel.DataTransfer.Clipboard.SetContent(package);
    }

    private void RenderAssistant(ChatMessage message)
    {
        if (message.IsStreaming)
        {
            // One element, updated in place. Formatting arrives when the answer does; see
            // MarkdownRenderer.StreamingText for why it cannot arrive sooner.
            if (_streamingText is null || !ReferenceEquals(ContentHost.Content, _streamingText))
            {
                _streamingText = new TextBlock
                {
                    Style = Themes.JarvisTheme.Style("MessageText"),
                    TextWrapping = TextWrapping.Wrap,
                    IsTextSelectionEnabled = true,
                };
                ContentHost.Content = _streamingText;
            }
            _streamingText.Text = MarkdownRenderer.StreamingText(message.Content, message.HasStructuredReasoning);
        }
        else
        {
            _streamingText = null;
            ContentHost.Content = MarkdownRenderer.Render(message.Content, false, message.HasStructuredReasoning);
        }

        StreamingCaret.Visibility = message.IsStreaming ? Visibility.Visible : Visibility.Collapsed;

        RenderToolSteps(message);
        RenderConfirmation(message);
        RenderCompare(message);
        RenderMetadata(message);
    }

    private void RenderCompare(ChatMessage message)
    {
        if (string.IsNullOrEmpty(message.CompareAlternative) || message.CompareVoted || message.IsStreaming)
        {
            CompareCard.Visibility = Visibility.Collapsed;
            CompareHost.Content = null;
            return;
        }
        CompareCard.Visibility = Visibility.Visible;
        var axis = string.IsNullOrEmpty(message.CompareAxis) ? "" : $" ({message.CompareAxis})";
        CompareTitle.Text = $"Another take{axis}. Which reads better?";
        CompareHost.Content = MarkdownRenderer.Render(message.CompareAlternative, false, false);
    }

    private void CompareVote_Click(object sender, RoutedEventArgs e)
    {
        if (_message is { } message && sender is Button { Tag: string choice })
        {
            CompareVoteRequested?.Invoke(message, choice);
        }
    }

    /// <summary>The turn as it happened (PLAN 4.23): each stretch of thinking before the tool
    /// step it led to, the last one before the answer.</summary>
    private void RenderToolSteps(ChatMessage message)
    {
        var trace = (message, message.TraceVersion, message.ToolSteps.Count);
        if (_builtTrace == trace) return;
        _builtTrace = trace;

        if (message.ToolSteps.Count == 0 && message.Thinking.Count == 0)
        {
            ToolStepsHost.Visibility = Visibility.Collapsed;
            ToolStepsHost.Items.Clear();
            return;
        }
        ToolStepsHost.Visibility = Visibility.Visible;
        ToolStepsHost.Items.Clear();
        for (var i = 0; i <= message.ToolSteps.Count; i++)
        {
            foreach (var segment in message.Thinking.Where(t => t.BeforeStep == i && !string.IsNullOrWhiteSpace(t.Text)))
            {
                ToolStepsHost.Items.Add(new ThinkingCard(segment));
            }
            if (i == message.ToolSteps.Count) break;
            var step = message.ToolSteps[i];
            ToolStepsHost.Items.Add(ToolStepText.Memory(step) is { } line
                ? new MemoryLineCard(step, line, UndoMemory)
                : new ToolStepCard(step));
        }
    }

    private void RenderConfirmation(ChatMessage message)
    {
        var confirmation = message.PendingConfirmations.FirstOrDefault();
        if (confirmation is null)
        {
            ConfirmationBanner.Visibility = Visibility.Collapsed;
            return;
        }

        ConfirmationBanner.Visibility = Visibility.Visible;
        ConfirmationBanner.Tag = confirmation;
        ConfirmationTitle.Text = $"I need your approval to run: {confirmation.Tool}" +
            (string.IsNullOrEmpty(confirmation.RiskTier) ? "" : $" ({confirmation.RiskTier})");
        ConfirmationReason.Text = confirmation.Reason ?? "";
        try
        {
            ConfirmationArgs.Text = confirmation.ArgsRaw.ValueKind == JsonValueKind.Object
                ? DescribeArgs(confirmation.ArgsRaw)
                : JsonSerializer.Serialize(confirmation.ArgsRaw, new JsonSerializerOptions { WriteIndented = true });
        }
        catch
        {
            ConfirmationArgs.Text = confirmation.ArgsRaw.ToString();
        }
    }

    private const int PreviewLines = 10;
    private const int PreviewChars = 600;

    /// <summary>One "name: value" line per argument. Text the model wrote (a write's content, a
    /// patch's blocks) shows as its first lines, so the user sees what will be saved -- as JSON it
    /// was one escaped line of the whole summary.</summary>
    private static string DescribeArgs(JsonElement args)
    {
        var lines = new List<string>();
        foreach (var arg in args.EnumerateObject())
        {
            if (arg.Value.ValueKind != JsonValueKind.String)
            {
                lines.Add($"{arg.Name}: {arg.Value.GetRawText()}");
                continue;
            }
            var text = arg.Value.GetString() ?? "";
            if (!text.Contains('\n') && text.Length <= 120)
            {
                lines.Add($"{arg.Name}: {text}");
                continue;
            }
            var all = text.Replace("\r\n", "\n").Split('\n');
            var shown = string.Join("\n", all.Take(PreviewLines));
            if (shown.Length > PreviewChars) shown = shown[..PreviewChars] + "…";
            var rest = all.Length - PreviewLines;
            lines.Add($"{arg.Name}:\n{shown}" + (rest > 0 ? $"\n… {rest} more line{(rest == 1 ? "" : "s")}" : ""));
        }
        return string.Join("\n", lines);
    }

    private void RenderMetadata(ChatMessage message)
    {
        if (string.IsNullOrEmpty(message.Model) && string.IsNullOrEmpty(message.Provider))
        {
            MetadataFooter.Visibility = Visibility.Collapsed;
            return;
        }
        MetadataFooter.Visibility = Visibility.Visible;
        // Live replies carry the turn's metrics (PLAN 4.13 #9): model, first-token time, speed,
        // rounds, tools, prompt/cache sizes and any safety net that fired. Reloaded history has
        // none, and keeps the old provider · model line.
        MetadataFooter.Text = message.Metrics is not null
            ? TurnMetrics.Format(message.Model, message.Metrics)
            : string.Join(" · ", new[] { message.Provider, message.Model }.Where(s => !string.IsNullOrEmpty(s)));
        // The alias alone ("main"/"fast") doesn't say which actual GGUF answered or why -- the
        // router's own reason string does (e.g. "...routed to FAST_MODEL (Qwen3.5-4B-...)."), so
        // surface it as a tooltip rather than cluttering the footer text itself.
        ToolTipService.SetToolTip(MetadataFooter, message.RouteReason);
    }

    private void ApproveButton_Click(object sender, RoutedEventArgs e)
    {
        if (ConfirmationBanner.Tag is PendingConfirmation confirmation)
        {
            ApproveRequested?.Invoke(confirmation);
        }
    }

    private void DenyButton_Click(object sender, RoutedEventArgs e)
    {
        if (ConfirmationBanner.Tag is PendingConfirmation confirmation)
        {
            DenyRequested?.Invoke(confirmation);
        }
    }
}
