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
/// Binding/DataContext propagation from the hosting ListView's DataTemplate — see the note on
/// MainWindow.xaml's MessageList template.
/// </summary>
public sealed partial class MessageBubbleControl : UserControl
{
    public event Action<PendingConfirmation>? ApproveRequested;
    public event Action<PendingConfirmation>? DenyRequested;

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
    private (string Content, bool Streaming, int ToolSteps, int Confirmations, string? Model)? _rendered;

    public MessageBubbleControl()
    {
        InitializeComponent();

        _renderThrottle = DispatcherQueue.CreateTimer();
        _renderThrottle.IsRepeating = false;
        _renderThrottle.Tick += (_, _) => Render();

        DataContextChanged += (_, _) => Bind(DataContext as ChatMessage);
        // The ListView recycles these controls; a timer left running would render into a
        // container that has since been handed to a different message.
        Unloaded += (_, _) => _renderThrottle.Stop();
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
        RenderNow();
    }

    private void OnMessagePropertyChanged(object? sender, PropertyChangedEventArgs e)
    {
        switch (e.PropertyName)
        {
            // Streaming starting or finishing is a state the user must see immediately: the
            // final token's text, the caret going away, the tool steps and footer appearing.
            case nameof(ChatMessage.IsStreaming):
                DispatcherQueue.TryEnqueue(RenderNow);
                break;

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

        var signature = (message.Content, message.IsStreaming, message.ToolSteps.Count,
                         message.PendingConfirmations.Count, message.Model);
        if (_rendered == signature) return;
        _rendered = signature;

        SystemPill.Visibility = Visibility.Collapsed;
        UserRow.Visibility = Visibility.Collapsed;
        AssistantRow.Visibility = Visibility.Collapsed;

        switch (message.Role)
        {
            case MessageRole.System:
                SystemPill.Visibility = Visibility.Visible;
                SystemText.Text = message.Content;
                break;

            case MessageRole.User:
                UserRow.Visibility = Visibility.Visible;
                UserText.Text = message.Content;
                break;

            default:
                AssistantRow.Visibility = Visibility.Visible;
                RenderAssistant(message);
                break;
        }
    }

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
                    TextWrapping = TextWrapping.Wrap,
                    FontSize = 14,
                    Foreground = new SolidColorBrush(Color.FromArgb(255, 243, 247, 252)),
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
        RenderMetadata(message);
    }

    private void RenderToolSteps(ChatMessage message)
    {
        if (message.ToolSteps.Count == 0)
        {
            ToolStepsHost.Visibility = Visibility.Collapsed;
            return;
        }
        ToolStepsHost.Visibility = Visibility.Visible;
        ToolStepsHost.Items.Clear();
        foreach (var step in message.ToolSteps)
        {
            ToolStepsHost.Items.Add(new ToolStepCard(step));
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
            ConfirmationArgs.Text = JsonSerializer.Serialize(confirmation.ArgsRaw, new JsonSerializerOptions { WriteIndented = true });
        }
        catch
        {
            ConfirmationArgs.Text = confirmation.ArgsRaw.ToString();
        }
    }

    private void RenderMetadata(ChatMessage message)
    {
        if (string.IsNullOrEmpty(message.Model) && string.IsNullOrEmpty(message.Provider))
        {
            MetadataFooter.Visibility = Visibility.Collapsed;
            return;
        }
        MetadataFooter.Visibility = Visibility.Visible;
        MetadataFooter.Text = string.Join(" · ", new[] { message.Provider, message.Model }.Where(s => !string.IsNullOrEmpty(s)));
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
