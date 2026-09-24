using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;

namespace Jarvis_Glass;

/// <summary>
/// Keeps glass shapes in step with XAML scrolling. A ScrollViewer animates its offset on the
/// compositor while glass shapes are published from the UI thread into the separate glass
/// HWND, so during a smooth scroll the two drift in and out of phase and a glass thumb
/// visibly shakes against its XAML label. Two halves fix that: <see cref="ImmediateProperty"/>
/// makes wheel scrolling step without animation (both pipelines jump on the same frame), and
/// <see cref="Track"/> republishes a control whenever its ancestor ScrollViewer's view changes
/// (LayoutUpdated is not guaranteed to fire for a pure offset change).
/// </summary>
public static class GlassScroll
{
    public static readonly DependencyProperty ImmediateProperty = DependencyProperty.RegisterAttached(
        "Immediate", typeof(bool), typeof(GlassScroll), new PropertyMetadata(false, OnImmediateChanged));

    public static bool GetImmediate(DependencyObject d) => (bool)d.GetValue(ImmediateProperty);
    public static void SetImmediate(DependencyObject d, bool value) => d.SetValue(ImmediateProperty, value);

    private static void OnImmediateChanged(DependencyObject d, DependencyPropertyChangedEventArgs e)
    {
        if (d is not ScrollViewer sv) return;
        if ((bool)e.NewValue) sv.PointerWheelChanged += OnWheel;
        else sv.PointerWheelChanged -= OnWheel;
    }

    private static void OnWheel(object sender, PointerRoutedEventArgs e)
    {
        var sv = (ScrollViewer)sender;
        if (sv.ScrollableHeight <= 0) return;
        var delta = e.GetCurrentPoint(sv).Properties.MouseWheelDelta;
        // One wheel notch (120) scrolls 48 DIPs, close to the stock animated distance.
        var target = Math.Clamp(sv.VerticalOffset - delta * 0.4, 0, sv.ScrollableHeight);
        sv.ChangeView(null, target, null, disableAnimation: true);
        e.Handled = true;
    }

    /// <summary>Republish <paramref name="element"/>'s shapes on every view change of its nearest
    /// ancestor ScrollViewer, and whenever something moves or fades it (<see cref="GlassMotion"/>).
    /// Every glass control calls this from Loaded; the hooks release themselves on Unloaded.</summary>
    public static void Track(FrameworkElement element, Action republish)
    {
        GlassMotion.Track(element, republish);
        DependencyObject? node = element;
        ScrollViewer? sv = null;
        while (node is not null && sv is null)
        {
            node = Microsoft.UI.Xaml.Media.VisualTreeHelper.GetParent(node);
            sv = node as ScrollViewer;
        }
        if (sv is null) return;

        void OnViewChanged(object? s, ScrollViewerViewChangedEventArgs e) => republish();
        sv.ViewChanged += OnViewChanged;
        void OnUnloaded(object s, RoutedEventArgs e)
        {
            sv.ViewChanged -= OnViewChanged;
            element.Unloaded -= OnUnloaded;
        }
        element.Unloaded += OnUnloaded;
    }
}
