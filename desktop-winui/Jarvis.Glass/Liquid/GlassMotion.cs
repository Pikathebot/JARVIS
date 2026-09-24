using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Media;

namespace Jarvis_Glass;

/// <summary>
/// Lets glass follow XAML that moves or fades. Glass is published from each control's own
/// bounds, and two kinds of change never reach it on their own: a RenderTransform (no
/// LayoutUpdated) and opacity (glass has none -- XAML Opacity fades only the XAML). So:
/// <list type="bullet">
/// <item><see cref="OpacityProperty"/> is glass opacity, set alongside XAML Opacity by whoever
/// animates. <see cref="GlassScene"/> multiplies it down the tree into every shape (the shaders'
/// fade, <c>Params3.w</c>) and text run it publishes. Kept separate from XAML Opacity on purpose:
/// an open sheet fades the window's text to 8% while the panels' glass must stay (see
/// MainWindow.SetSheetOpen).</item>
/// <item><see cref="Notify"/> republishes the glass controls inside an element that moved or
/// faded; <see cref="GlassTransition"/> calls it for its element every frame it animates.</item>
/// <item><see cref="Frame"/> is the animation tick. Each <see cref="GlassHost"/> runs it at the
/// top of its render tick, so what an animation moves is rendered in the SAME frame; a plain
/// CompositionTarget.Rendering handler subscribed after the host's would run after the glass
/// was drawn and leave it one frame behind the XAML.</item>
/// </list>
/// Cost is kept off the hot path: every control publishes on every layout pass (LayoutUpdated
/// fires per token while a reply streams), and each tree walk is a WinRT call per level. So a
/// control's ancestors are captured once when it loads, opacity is read from that set, and when
/// nothing is faded -- nearly always -- <see cref="OpacityFor"/> returns 1 without touching WinRT.
/// </summary>
public static class GlassMotion
{
    public static readonly DependencyProperty OpacityProperty = DependencyProperty.RegisterAttached(
        "Opacity", typeof(double), typeof(GlassMotion), new PropertyMetadata(1.0, OnOpacityChanged));

    /// <summary>Glass opacity of the element's subtree (multiplies down the tree, like XAML Opacity).</summary>
    public static double GetOpacity(DependencyObject d) => (double)d.GetValue(OpacityProperty);
    public static void SetOpacity(DependencyObject d, double value) => d.SetValue(OpacityProperty, value);

    /// <summary>Elements whose glass opacity isn't 1, with that opacity.</summary>
    private static readonly Dictionary<DependencyObject, double> Faded = new();

    private static void OnOpacityChanged(DependencyObject d, DependencyPropertyChangedEventArgs e)
    {
        var value = (double)e.NewValue;
        if (value >= 1.0) Faded.Remove(d);
        else Faded[d] = value;
        if (!_stepping && d is UIElement element) Notify(element);
    }

    /// <summary>The product of <see cref="OpacityProperty"/> over the element and its ancestors.</summary>
    public static float OpacityFor(UIElement element)
    {
        if (Faded.Count == 0) return 1f;
        var opacity = 1.0;
        if (TrackedByElement.TryGetValue(element, out var tracked))
        {
            foreach (var (faded, value) in Faded)
            {
                if (tracked.Ancestors.Contains(faded)) opacity *= value;
            }
        }
        else
        {
            // Not loaded yet (no captured ancestors): walk.
            DependencyObject? node = element;
            while (node is not null)
            {
                if (Faded.TryGetValue(node, out var value)) opacity *= value;
                node = VisualTreeHelper.GetParent(node);
            }
        }
        return (float)Math.Clamp(opacity, 0, 1);
    }

    /// <summary>Diagnostics: the faded elements, by name or type, with their glass opacity.</summary>
    public static string DescribeFaded() =>
        Faded.Count == 0 ? "none" : string.Join(", ", Faded.Select(kv =>
            $"{(kv.Key is FrameworkElement { Name.Length: > 0 } fe ? fe.Name : kv.Key.GetType().Name)}={kv.Value:F2}"));

    private sealed record Tracked(Action Republish, HashSet<DependencyObject> Ancestors);

    private static readonly Dictionary<UIElement, Tracked> TrackedByElement = new();

    /// <summary>Republish every tracked control inside <paramref name="moved"/> (itself included).
    /// Cheap for the rest: a publish identical to the last is a no-op in <see cref="GlassScene"/>.</summary>
    public static void Notify(UIElement moved)
    {
        _moved.Clear();
        _moved.Add(moved);
        NotifyMoved();
    }

    private static readonly HashSet<DependencyObject> _moved = new();

    private static void NotifyMoved()
    {
        if (_moved.Count == 0) return;
        // Snapshot: a republish can load or unload controls.
        foreach (var tracked in TrackedByElement.Values.ToArray())
        {
            if (tracked.Ancestors.Overlaps(_moved)) tracked.Republish();
        }
        _moved.Clear();
    }

    /// <summary>Republish <paramref name="element"/> whenever it or an ancestor moves or fades
    /// through here. Call from Loaded (the ancestors are captured then -- a loaded control
    /// doesn't change parents); the hook releases itself on Unloaded.</summary>
    public static void Track(FrameworkElement element, Action republish)
    {
        var ancestors = new HashSet<DependencyObject>();
        DependencyObject? node = element;
        while (node is not null)
        {
            ancestors.Add(node);
            node = VisualTreeHelper.GetParent(node);
        }
        TrackedByElement[element] = new Tracked(republish, ancestors);
        void OnUnloaded(object s, RoutedEventArgs e)
        {
            TrackedByElement.Remove(element);
            element.Unloaded -= OnUnloaded;
        }
        element.Unloaded += OnUnloaded;
    }

    // ---- the animation tick ------------------------------------------------------------------

    private static readonly List<(UIElement Element, Func<double, bool> Step)> Animators = new();
    private static TimeSpan _lastFrame = TimeSpan.MinValue;
    private static bool _fallbackHooked;
    private static bool _stepping;

    /// <summary>Runs <paramref name="step"/> once per display frame with the frame's time in
    /// seconds until it returns false, then republishes the glass inside
    /// <paramref name="element"/>. UI thread only.</summary>
    public static void Frame(UIElement element, Func<double, bool> step)
    {
        Animators.Add((element, step));
        // Fallback tick for frames no host renders (capture not started yet, window hidden):
        // an animation must still finish, or whatever awaits it never resumes.
        if (!_fallbackHooked)
        {
            _fallbackHooked = true;
            CompositionTarget.Rendering += OnRendering;
        }
    }

    private static void OnRendering(object? sender, object e) => Step(e);

    /// <summary>Advances every animator once for this frame. Called by each host before it
    /// renders and by the fallback hook; the frame's RenderingTime makes repeat calls (two
    /// hosted windows, then the fallback) no-ops.</summary>
    /// <summary>Holds every animation where it is (XAML and glass stay in step, since both only
    /// move here). For snapshots taken mid-transition; see GlassHost.Frozen.</summary>
    public static bool Paused { get; set; }

    internal static void Step(object renderingArgs)
    {
        if (renderingArgs is not RenderingEventArgs args || args.RenderingTime == _lastFrame) return;
        _lastFrame = args.RenderingTime;
        if (Paused) return; // on resume the first step's dt is clamped (GlassTransition), so nothing jumps
        if (Animators.Count == 0)
        {
            CompositionTarget.Rendering -= OnRendering;
            _fallbackHooked = false;
            return;
        }
        var now = args.RenderingTime.TotalSeconds;
        // One republish for the whole frame, after every animator has moved, not one per write.
        _stepping = true;
        try
        {
            _moved.Clear();
            for (var i = Animators.Count - 1; i >= 0; i--)
            {
                var (element, step) = Animators[i];
                _moved.Add(element);
                if (!step(now)) Animators.RemoveAt(i);
            }
        }
        finally
        {
            _stepping = false;
        }
        NotifyMoved();
    }
}
