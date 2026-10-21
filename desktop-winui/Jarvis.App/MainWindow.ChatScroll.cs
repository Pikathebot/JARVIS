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
    private bool _chatOwnScroll;   // a ChangeView of ours is in flight (its ViewChanged isn't the user's)
    private bool _chatUserScroll;  // a wheel, key, scrollbar or touch scroll is under way: its ViewChanged decides following

    /// <summary>Holding the end, or not -- and the anchor that goes with it. Anchored to the bottom
    /// (ratio 1) the view keeps the end in place as a reply grows; but scrolled up with it still
    /// at 1, each message the virtualizing list lays out below at a height other than its estimate
    /// moved the view, which pushed it back up while scrolling down (device 2026-10-21, a long
    /// thread with many tool rows). Not following, the top anchors (ratio 0): scrolling down
    /// realizes rows below without moving what's on screen, and older pages inserted at the top
    /// keep it in place too.</summary>
    private void SetChatFollow(bool follow)
    {
        if (_chatFollow == follow) return; // changing the anchor moves the view: only on a real change
        _chatFollow = follow;
        if (_chatScroller is not null) _chatScroller.VerticalAnchorRatio = follow ? 1.0 : 0.0;
    }

    /// <summary>How close to the top (DIPs) the thread's scroll-back fetches the page before.</summary>
    private const double ThreadScrollBackSlop = 300;

    private void SetUpChatFollow()
    {
        MessageScroll.Loaded += (_, _) => AttachChatScroller();
        ChatViewModel.Messages.CollectionChanged += OnChatMessagesChanged;
        ChatViewModel.ScrollToItemRequested += ScrollChatToItem;
    }

    private void AttachChatScroller()
    {
        if (_chatScroller is not null) return;
        _chatScroller = MessageScroll;

        // The reading column is placed here, not by MaxWidth + centring: inside this
        // ScrollViewer the ItemsRepeater got a slot ~232 DIP wider than the viewport and sat that
        // far off centre, right of the composer (device 2026-10-20).
        _chatScroller.SizeChanged += (_, _) => PlaceChatColumn();
        _chatScroller.RegisterPropertyChangedCallback(ScrollViewer.ViewportWidthProperty, (_, _) => PlaceChatColumn());
        MessageList.SizeChanged += (_, _) => LogChatColumnOnce();
        PlaceChatColumn();
        // The awareness tray floats over the transcript's bottom right: while it shows, the
        // transcript ends above it rather than under it (4.20: a VRAM toast covered the last line).
        _chatBottomPadding = _chatScroller.Padding.Bottom;
        AwarenessTrayHost.SizeChanged += (_, _) => ReserveRoomForTray();
        ReserveRoomForTray();

        // Content growing (a streamed token, a tool row expanding, a markdown re-render) shows up
        // as a larger ScrollableHeight; follow it if we are holding the end.
        _chatScroller.RegisterPropertyChangedCallback(ScrollViewer.ScrollableHeightProperty, (_, _) =>
        {
            if (_chatFollow) QueueChatScrollToEnd();
        });
        _chatScroller.ViewChanged += (_, e) =>
        {
            // The column must never sit sideways: anchoring once shifted it ~230 DIP right
            // (device 2026-10-20). HorizontalAnchorRatio=NaN turns that off; this is the backstop.
            if (Math.Abs(_chatScroller.HorizontalOffset) > 0.5)
            {
                App.Log($"chat scroller had a horizontal offset of {_chatScroller.HorizontalOffset:F1}; reset to 0");
                _chatScroller.ChangeView(0, null, null, disableAnimation: true);
            }
            // Only the user's own scrolling (wheel, keys, scrollbar, touch) decides whether we hold
            // the end: let go as soon as it leaves the end, even mid-drag, and take hold once it
            // settles there. ViewChanged also fires when the ScrollViewer's anchoring or the list
            // re-measuring a row moves the view; reading those as the user flipped following off
            // and on, each flip swapped the anchor and moved the view again -- the jitter that
            // kept the end out of reach (traced 2026-10-21: 71 DIP off the end with nobody
            // scrolling). While following, such a shift is put back at the end instead.
            if (_chatOwnScroll)
            {
                if (!e.IsIntermediate) _chatOwnScroll = false;
            }
            else if (_chatUserScroll)
            {
                if (!IsChatAtEnd()) SetChatFollow(false);
                else if (!e.IsIntermediate) SetChatFollow(true);
                if (!e.IsIntermediate) _chatUserScroll = false;
            }
            else if (_chatFollow && !IsChatAtEnd())
            {
                QueueChatScrollToEnd();
            }
            // The Freeform thread pages in older messages as the view nears its top. The
            // ScrollViewer's anchoring keeps what is on screen in place as they are inserted.
            if (ChatViewModel.IsThread && _chatScroller.VerticalOffset < ThreadScrollBackSlop
                && _chatScroller.ScrollableHeight > 0)
            {
                _ = ChatViewModel.LoadOlderThreadAsync();
            }
        };
        // A wheel or touchpad scroll upwards lets go at once, even mid-stream, rather than
        // waiting for the next ViewChanged to land (the stream may have moved the end by then).
        _chatScroller.AddHandler(UIElement.PointerWheelChangedEvent, new PointerEventHandler((_, e) =>
        {
            _chatUserScroll = true;
            if (e.GetCurrentPoint(_chatScroller).Properties.MouseWheelDelta > 0) SetChatFollow(false);
        }), handledEventsToo: true);
        _chatScroller.AddHandler(UIElement.KeyDownEvent, new KeyEventHandler((_, e) =>
        {
            if (e.Key is Windows.System.VirtualKey.Up or Windows.System.VirtualKey.Down
                or Windows.System.VirtualKey.PageUp or Windows.System.VirtualKey.PageDown
                or Windows.System.VirtualKey.Home or Windows.System.VirtualKey.End or Windows.System.VirtualKey.Space)
                _chatUserScroll = true;
        }), handledEventsToo: true);
        // Touch and precision-touchpad pans.
        _chatScroller.DirectManipulationStarted += (_, _) => _chatUserScroll = true;
        _chatScroller.DirectManipulationCompleted += (_, _) => SetChatFollow(IsChatAtEnd());
        // The scrollbar: thumb drags, arrow and track clicks.
        if (FindVerticalScrollBar(_chatScroller) is { } bar)
        {
            bar.Scroll += (_, e) =>
            {
                _chatUserScroll = true;
                if (e.ScrollEventType == Microsoft.UI.Xaml.Controls.Primitives.ScrollEventType.EndScroll)
                    SetChatFollow(IsChatAtEnd());
            };
        }
        else App.Log("chat scroller: no vertical scrollbar found; drags are seen through ViewChanged only");
        QueueChatScrollToEnd();
    }

    private void OnChatMessagesChanged(object? sender, NotifyCollectionChangedEventArgs e)
    {
        // A session opened (reset) or the user sent something: take hold of the end again.
        if (e.Action == NotifyCollectionChangedAction.Reset)
            SetChatFollow(true);
        // Only at the end: the thread's scroll-back inserts older user messages at the top.
        else if (e.Action == NotifyCollectionChangedAction.Add && e.NewItems is { } added
                 && e.NewStartingIndex + added.Count >= ChatViewModel.Messages.Count)
            foreach (var item in added)
                if (item is ChatMessage { Role: MessageRole.User }) SetChatFollow(true);

        if (_chatFollow) QueueChatScrollToEnd();
    }

    /// <summary>A sidebar episode row: bring its marker to the top of the view and let go of
    /// the end, so the next token doesn't pull the view back down.</summary>
    private void ScrollChatToItem(int index)
    {
        if (index < 0 || index >= ChatViewModel.Messages.Count) return;
        SetChatFollow(false);
        if (MessageList.GetOrCreateElement(index) is UIElement element)
        {
            MessageList.UpdateLayout();
            element.StartBringIntoView(new BringIntoViewOptions { VerticalAlignmentRatio = 0.0, VerticalOffset = -12, AnimationDesired = true });
        }
    }

    private double _chatBottomPadding;

    private void ReserveRoomForTray()
    {
        if (_chatScroller is null) return;
        var tray = AwarenessTrayHost.Visibility == Visibility.Visible ? AwarenessTrayHost.ActualHeight : 0;
        var bottom = _chatBottomPadding + (tray > 0 ? tray + AwarenessTrayHost.Margin.Bottom : 0);
        var p = _chatScroller.Padding;
        if (Math.Abs(p.Bottom - bottom) < 0.5) return;
        _chatScroller.Padding = new Thickness(p.Left, p.Top, p.Right, bottom);
        if (_chatFollow) QueueChatScrollToEnd();
    }

    /// <summary>The transcript's reading width, the same as the composer's (MainWindow.xaml).</summary>
    private const double ChatColumnWidth = 720;

    private void PlaceChatColumn()
    {
        if (_chatScroller is null || _chatScroller.ViewportWidth <= 0) return;
        var width = Math.Min(ChatColumnWidth, _chatScroller.ViewportWidth);
        var left = Math.Floor((_chatScroller.ViewportWidth - width) / 2);
        if (MessageList.Width != width) MessageList.Width = width;
        if (MessageList.Margin.Left != left) MessageList.Margin = new Thickness(left, 0, 0, 0);
    }

    private bool _chatColumnLogged;

    /// <summary>One line per run with the real numbers, to confirm where the column landed.</summary>
    private void LogChatColumnOnce()
    {
        if (_chatColumnLogged || _chatScroller is null || MessageList.ActualWidth <= 0) return;
        _chatColumnLogged = true;
        var at = MessageList.TransformToVisual(_chatScroller).TransformPoint(new Windows.Foundation.Point(0, 0));
        var composer = ComposerArea.TransformToVisual(_chatScroller).TransformPoint(new Windows.Foundation.Point(0, 0));
        App.Log($"chat column: scroller {_chatScroller.ActualWidth:F0} viewport {_chatScroller.ViewportWidth:F0} " +
                $"extent {_chatScroller.ExtentWidth:F0} hoffset {_chatScroller.HorizontalOffset:F0}; list at x {at.X:F0} " +
                $"width {MessageList.ActualWidth:F0}; composer at x {composer.X:F0} width {ComposerArea.ActualWidth:F0}");
    }

    private static Microsoft.UI.Xaml.Controls.Primitives.ScrollBar? FindVerticalScrollBar(DependencyObject root)
    {
        for (var i = 0; i < Microsoft.UI.Xaml.Media.VisualTreeHelper.GetChildrenCount(root); i++)
        {
            var child = Microsoft.UI.Xaml.Media.VisualTreeHelper.GetChild(root, i);
            if (child is Microsoft.UI.Xaml.Controls.Primitives.ScrollBar { Orientation: Orientation.Vertical } bar) return bar;
            if (child is ItemsRepeater) continue; // the transcript itself holds no scrollbar of the viewer's
            if (FindVerticalScrollBar(child) is { } found) return found;
        }
        return null;
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
            {
                _chatOwnScroll = true;
                if (!_chatScroller.ChangeView(null, _chatScroller.ScrollableHeight, null, disableAnimation: true))
                    _chatOwnScroll = false; // refused: no ViewChanged will come to clear it
            }
        });
    }
}
