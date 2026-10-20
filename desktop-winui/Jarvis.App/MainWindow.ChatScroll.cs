using System.Collections.Specialized;
using Jarvis.Core.Models;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;

namespace Jarvis_App;

/// <summary>
/// The chat follows its end: while the view sits at the bottom, a new message or a reply growing
/// token by token keeps it there. Scrolling up lets go; scrolling back to the bottom, sending a
/// message or opening a session takes hold again. Without this the view kept the *first*
/// visible row in place, so once a conversation outgrew the window every new reply landed below
/// the fold (seen on device 2026-10-20).
///
/// Most of the following is done by the ScrollViewer itself: VerticalAnchorRatio="1" (MainWindow.xaml)
/// keeps a view that sits at the end at the end while the content grows, inside the same layout
/// pass, so nothing is drawn one frame behind. Scrolling from here (a queued ChangeView) is for
/// what anchoring doesn't cover -- taking hold again after a send or a session switch -- and as
/// a backstop; on its own it lagged a frame behind each token and jittered (device 2026-10-20).
/// </summary>
public sealed partial class MainWindow
{
    /// <summary>How close to the bottom (DIPs) still counts as "at the end".</summary>
    private const double ChatFollowSlop = 32;

    private ScrollViewer? _chatScroller;
    private bool _chatFollow = true;
    private bool _chatScrollQueued;

    private void SetUpChatFollow()
    {
        MessageScroll.Loaded += (_, _) => AttachChatScroller();
        ChatViewModel.Messages.CollectionChanged += OnChatMessagesChanged;
    }

    private void AttachChatScroller()
    {
        if (_chatScroller is not null) return;
        _chatScroller = MessageScroll;

        // Content growing (a streamed token, a tool row expanding, a markdown re-render) shows up
        // as a larger ScrollableHeight; follow it if we are holding the end.
        _chatScroller.RegisterPropertyChangedCallback(ScrollViewer.ScrollableHeightProperty, (_, _) =>
        {
            if (_chatFollow) QueueChatScrollToEnd();
        });
        _chatScroller.ViewChanged += (_, e) =>
        {
            if (e.IsIntermediate || _chatScrollQueued) return;
            _chatFollow = IsChatAtEnd();
        };
        // A wheel or touchpad scroll upwards lets go at once, even mid-stream, rather than
        // waiting for the next ViewChanged to land (the stream may have moved the end by then).
        _chatScroller.AddHandler(UIElement.PointerWheelChangedEvent, new PointerEventHandler((_, e) =>
        {
            if (e.GetCurrentPoint(_chatScroller).Properties.MouseWheelDelta > 0) _chatFollow = false;
        }), handledEventsToo: true);
        QueueChatScrollToEnd();
    }

    private void OnChatMessagesChanged(object? sender, NotifyCollectionChangedEventArgs e)
    {
        // A session opened (reset) or the user sent something: take hold of the end again.
        if (e.Action == NotifyCollectionChangedAction.Reset)
            _chatFollow = true;
        else if (e.Action == NotifyCollectionChangedAction.Add && e.NewItems is { } added)
            foreach (var item in added)
                if (item is ChatMessage { Role: MessageRole.User }) _chatFollow = true;

        if (_chatFollow) QueueChatScrollToEnd();
    }

    private bool IsChatAtEnd() =>
        _chatScroller is null || _chatScroller.VerticalOffset >= _chatScroller.ScrollableHeight - ChatFollowSlop;

    /// <summary>One scroll per layout pass: many tokens can land between frames.</summary>
    private void QueueChatScrollToEnd()
    {
        if (_chatScrollQueued || _chatScroller is null) return;
        _chatScrollQueued = true;
        DispatcherQueue.TryEnqueue(Microsoft.UI.Dispatching.DispatcherQueuePriority.Low, () =>
        {
            _chatScrollQueued = false;
            if (_chatScroller is null || !_chatFollow) return;
            // Settle layout first so ScrollableHeight is this frame's, not the one before the
            // last token: scrolling to a stale end and correcting a frame later was the jitter.
            _chatScroller.UpdateLayout();
            if (_chatScroller.VerticalOffset < _chatScroller.ScrollableHeight - 0.5)
                _chatScroller.ChangeView(null, _chatScroller.ScrollableHeight, null, disableAnimation: true);
        });
    }
}
