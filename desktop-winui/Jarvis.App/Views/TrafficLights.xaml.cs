using Jarvis_Glass;
using Microsoft.UI.Input;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Shapes;

namespace Jarvis_App.Views;

/// <summary>
/// Traffic-light window buttons for a window with a collapsed system title bar (the Settings
/// window; the main window keeps its own copy in MainWindow.WindowControls.cs, which this
/// follows). <see cref="Attach"/> registers the three dots as the window's Minimize / Maximize /
/// Close regions and everything left of them in the top <c>captionHeight</c> DIPs as the drag
/// caption, so Windows does the clicking, Snap layouts and dragging.
/// </summary>
public sealed partial class TrafficLights : UserControl
{
    private static readonly NonClientRegionKind[] Kinds =
        { NonClientRegionKind.Minimize, NonClientRegionKind.Maximize, NonClientRegionKind.Close };

    private Window? _window;
    private AppWindow? _appWindow;
    private FrameworkElement? _root;
    private InputNonClientPointerSource? _source;
    private double _captionHeight = 48;
    private NonClientRegionKind? _hovered;
    private NonClientRegionKind? _pressed;
    private bool _focused = true;
    private Windows.Graphics.RectInt32[][] _rects = Array.Empty<Windows.Graphics.RectInt32[]>();
    private int _captionWidth = -1;

    /// <summary>Set as the window starts closing: from then on nothing touches its non-client
    /// regions (an exception from a layout-pass handler on a dying window is a native crash).</summary>
    private bool _closed;

    public TrafficLights()
    {
        InitializeComponent();
    }

    public void Attach(Window window, AppWindow appWindow, FrameworkElement root, double captionHeight = 48)
    {
        _window = window;
        _appWindow = appWindow;
        _root = root;
        _captionHeight = captionHeight;
        if (!AppWindowTitleBar.IsCustomizationSupported()) return;

        appWindow.TitleBar.ExtendsContentIntoTitleBar = true;
        appWindow.TitleBar.PreferredHeightOption = TitleBarHeightOption.Collapsed;
        _source = InputNonClientPointerSource.GetForWindowId(appWindow.Id);
        _source.PointerEntered += (_, e) => OnPointer(e.RegionKind, entered: true);
        _source.PointerExited += (_, e) => OnPointer(e.RegionKind, entered: false);
        _source.PointerPressed += (_, e) => OnPress(e.RegionKind, pressed: true);
        _source.PointerReleased += (_, e) => OnPress(e.RegionKind, pressed: false);
        // Windows resets these regions after some activations (microsoft-ui-xaml #10379).
        _source.RegionsChanged += (_, e) =>
        {
            if (e.ChangedRegions.Any(k => Kinds.Contains(k)))
                DispatcherQueue.TryEnqueue(() => ApplyRegions(force: false));
        };

        window.Activated += (_, e) =>
        {
            _focused = e.WindowActivationState != WindowActivationState.Deactivated;
            UpdateLooks();
        };
        appWindow.Changed += (_, e) =>
        {
            if (e.DidPresenterChange || e.DidSizeChange) UpdateLooks();
        };
        Action onPalette = () => DispatcherQueue.TryEnqueue(UpdateLooks);
        JarvisPalette.Changed += onPalette;
        window.Closed += (_, _) => JarvisPalette.Changed -= onPalette;
        root.LayoutUpdated += OnRootLayoutUpdated;
        appWindow.Closing += (_, _) => Shutdown();
        window.Closed += (_, _) => Shutdown();
        UpdateLooks();
    }

    private void Shutdown()
    {
        _closed = true;
        if (_root is not null) _root.LayoutUpdated -= OnRootLayoutUpdated;
    }

    private void OnRootLayoutUpdated(object? sender, object e)
    {
        try
        {
            UpdateRegions();
        }
        catch (Exception ex)
        {
            App.Log($"traffic lights: region update failed: {ex.Message}");
        }
    }

    /// <summary>On every layout pass; cheap when nothing moved.</summary>
    private void UpdateRegions()
    {
        if (_closed || _source is null || _root?.XamlRoot is null || MinimizeHit.ActualWidth <= 0) return;
        var scale = _root.XamlRoot.RasterizationScale;

        Windows.Graphics.RectInt32 Rect(FrameworkElement e, bool toCorner)
        {
            var b = e.TransformToVisual(_root).TransformBounds(new Windows.Foundation.Rect(0, 0, e.ActualWidth, e.ActualHeight));
            if (toCorner) b = new Windows.Foundation.Rect(b.X, 0, _root.ActualWidth - b.X, b.Bottom);
            return new Windows.Graphics.RectInt32(
                (int)Math.Round(b.X * scale), (int)Math.Round(b.Y * scale),
                (int)Math.Round(b.Width * scale), (int)Math.Round(b.Height * scale));
        }

        var rects = new[] { new[] { Rect(MinimizeHit, false) }, new[] { Rect(MaximizeHit, false) }, new[] { Rect(CloseHit, true) } };
        var left = MinimizeHit.TransformToVisual(_root).TransformPoint(new Windows.Foundation.Point(0, 0)).X;
        var captionWidth = (int)Math.Round(left * scale);
        var same = rects.Length == _rects.Length && rects.Zip(_rects).All(p => p.First.SequenceEqual(p.Second));
        if (same && captionWidth == _captionWidth) return;

        _rects = rects;
        _captionWidth = captionWidth;
        ApplyRegions(force: true);
        _source.SetRegionRects(NonClientRegionKind.Caption, new[]
        {
            new Windows.Graphics.RectInt32(0, 0, captionWidth, (int)Math.Round(_captionHeight * scale)),
        });
    }

    private void ApplyRegions(bool force)
    {
        if (_closed || _source is null || _rects.Length != Kinds.Length) return;
        for (var i = 0; i < Kinds.Length; i++)
        {
            if (force || !_source.GetRegionRects(Kinds[i]).SequenceEqual(_rects[i]))
                _source.SetRegionRects(Kinds[i], _rects[i]);
        }
    }

    private void OnPointer(NonClientRegionKind kind, bool entered)
    {
        if (!Kinds.Contains(kind)) return;
        DispatcherQueue.TryEnqueue(() =>
        {
            if (entered) _hovered = kind;
            else if (_hovered == kind) { _hovered = null; _pressed = null; }
            UpdateLooks();
        });
    }

    private void OnPress(NonClientRegionKind kind, bool pressed)
    {
        if (!Kinds.Contains(kind)) return;
        DispatcherQueue.TryEnqueue(() =>
        {
            _pressed = pressed ? kind : null;
            UpdateLooks();
        });
    }

    private void UpdateLooks()
    {
        if (_closed) return;
        var colors = JarvisPalette.Current;
        var highContrast = JarvisPalette.Appearance == JarvisAppearance.HighContrast;
        var lit = _focused || _hovered is not null;
        var glyphs = lit && (_hovered is not null || highContrast);
        var maximized = (_appWindow?.Presenter as OverlappedPresenter)?.State == OverlappedPresenterState.Maximized;

        void Style(Ellipse dot, Ellipse press, UIElement glyph, Windows.UI.Color color, NonClientRegionKind kind)
        {
            dot.Fill = new SolidColorBrush(lit ? color : colors.WindowControlInactive);
            dot.Stroke = highContrast ? new SolidColorBrush(colors.Label) : null;
            dot.StrokeThickness = highContrast ? 1 : 0;
            press.Opacity = _pressed == kind ? 0.22 : 0;
            glyph.Opacity = glyphs ? 1 : 0;
        }

        Style(MinimizeDot, MinimizePress, MinimizeGlyph, colors.WindowMinimize, NonClientRegionKind.Minimize);
        Style(MaximizeDot, MaximizePress, MaximizeGlyph, colors.WindowMaximize, NonClientRegionKind.Maximize);
        Style(CloseDot, ClosePress, CloseGlyph, colors.WindowClose, NonClientRegionKind.Close);
        MaximizeOut.Visibility = maximized ? Visibility.Collapsed : Visibility.Visible;
        MaximizeIn.Visibility = maximized ? Visibility.Visible : Visibility.Collapsed;
        Microsoft.UI.Xaml.Automation.AutomationProperties.SetName(MaximizeHit, maximized ? "Restore down" : "Maximize");
    }
}
