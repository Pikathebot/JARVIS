using Jarvis_Glass;
using Microsoft.UI.Input;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Shapes;

namespace Jarvis_App;

/// <summary>
/// The traffic-light window buttons (PLAN 4.7d; the design is the "Jarvis Traffic-Light
/// Buttons" canvas). The system caption buttons are hidden and the three dots in
/// <c>WindowControls</c> take their place, but Windows still does the work: each dot's
/// rectangle is registered as the window's Minimize / Maximize / Close non-client region, so
/// clicks, the maximize/restore toggle, Snap layouts on green and close-to-tray (the
/// AppWindow.Closing handler) all behave exactly as the system buttons did. Non-client regions
/// take all pointer input, so the dots' looks are driven by the non-client pointer events.
///
/// Looks (as macOS): plain dots at rest; hovering any of them shows all three glyphs; a pressed
/// dot darkens; grey while another window is in front, colour back under the pointer; green
/// shows the restore glyph while maximized. High contrast always shows the glyphs and rings
/// each dot, so they are told apart by shape and not only by hue.
/// </summary>
public sealed partial class MainWindow
{
    private NonClientRegionKind? _hoveredControl;
    private NonClientRegionKind? _pressedControl;
    private bool _windowFocused = true;
    private Windows.Graphics.RectInt32[][] _controlRects = Array.Empty<Windows.Graphics.RectInt32[]>();
    private bool _reapplyQueued;

    private static readonly NonClientRegionKind[] ControlKinds =
        { NonClientRegionKind.Minimize, NonClientRegionKind.Maximize, NonClientRegionKind.Close };

    /// <summary>Hide the system caption buttons and hook up the dots. After ExtendTitleBar.</summary>
    private void SetUpWindowControls(InputNonClientPointerSource source)
    {
        // The system caption buttons go; the border (resize, shadow, rounded corners) and the
        // window's styles stay, so Snap layouts still work. The caption row drags through the
        // Caption region. (OverlappedPresenter.SetBorderAndTitleBar(true, false) left the
        // system buttons drawn behind the dots with an extended title bar.)
        _appWindow.TitleBar.PreferredHeightOption = TitleBarHeightOption.Collapsed;

        source.PointerEntered += (_, e) => OnControlPointer(e.RegionKind, entered: true);
        source.PointerExited += (_, e) => OnControlPointer(e.RegionKind, entered: false);
        source.PointerPressed += (_, e) => OnControlPress(e.RegionKind, pressed: true);
        source.PointerReleased += (_, e) => OnControlPress(e.RegionKind, pressed: false);
        // Windows resets these regions to its defaults after some activations (microsoft-ui-xaml
        // #10379 / #9281: "secretly reverts"), which would put the buttons back where the
        // system ones were. Put ours back whenever that happens.
        source.RegionsChanged += (_, e) =>
        {
            if (!e.ChangedRegions.Any(k => ControlKinds.Contains(k)) || _reapplyQueued) return;
            _reapplyQueued = true;
            DispatcherQueue.TryEnqueue(() =>
            {
                _reapplyQueued = false;
                ApplyControlRegions(source, force: false);
            });
        };

        Activated += (_, e) =>
        {
            _windowFocused = e.WindowActivationState != WindowActivationState.Deactivated;
            UpdateWindowControls();
        };
        _appWindow.Changed += (_, e) =>
        {
            if (e.DidPresenterChange || e.DidSizeChange) UpdateWindowControls();
        };
        JarvisPalette.Changed += () => DispatcherQueue.TryEnqueue(UpdateWindowControls);
        UpdateWindowControls();
    }

    private void OnControlPointer(NonClientRegionKind kind, bool entered)
    {
        if (!ControlKinds.Contains(kind)) return;
        DispatcherQueue.TryEnqueue(() =>
        {
            if (entered) _hoveredControl = kind;
            else if (_hoveredControl == kind) { _hoveredControl = null; _pressedControl = null; }
            UpdateWindowControls();
        });
    }

    private void OnControlPress(NonClientRegionKind kind, bool pressed)
    {
        if (!ControlKinds.Contains(kind)) return;
        DispatcherQueue.TryEnqueue(() =>
        {
            _pressedControl = pressed ? kind : null;
            UpdateWindowControls();
        });
    }

    /// <summary>The three regions in physical pixels: each dot's 23 x 28 cell, and close's
    /// reaching the top and right edges so the pointer thrown into the corner still closes.</summary>
    private Windows.Graphics.RectInt32[][] ComputeControlRects(double scale)
    {
        Windows.Graphics.RectInt32 Rect(FrameworkElement e, bool toCorner)
        {
            var b = e.TransformToVisual(RootGrid).TransformBounds(new Windows.Foundation.Rect(0, 0, e.ActualWidth, e.ActualHeight));
            if (toCorner) b = new Windows.Foundation.Rect(b.X, 0, RootGrid.ActualWidth - b.X, b.Bottom);
            return new Windows.Graphics.RectInt32(
                (int)Math.Round(b.X * scale), (int)Math.Round(b.Y * scale),
                (int)Math.Round(b.Width * scale), (int)Math.Round(b.Height * scale));
        }
        return new[]
        {
            new[] { Rect(MinimizeHit, false) },
            new[] { Rect(MaximizeHit, false) },
            new[] { Rect(CloseHit, true) },
        };
    }

    private void ApplyControlRegions(InputNonClientPointerSource source, bool force)
    {
        if (_controlRects.Length != ControlKinds.Length) return;
        for (var i = 0; i < ControlKinds.Length; i++)
        {
            if (force || !source.GetRegionRects(ControlKinds[i]).SequenceEqual(_controlRects[i]))
                source.SetRegionRects(ControlKinds[i], _controlRects[i]);
        }
    }

    /// <summary>Called from UpdateTitleBarRegions on every layout pass; cheap when nothing moved.</summary>
    private void UpdateControlRegions(InputNonClientPointerSource source, double scale)
    {
        if (MinimizeHit.ActualWidth <= 0) return;
        var rects = ComputeControlRects(scale);
        var same = rects.Length == _controlRects.Length
                   && rects.Zip(_controlRects).All(p => p.First.SequenceEqual(p.Second));
        if (same) return;
        _controlRects = rects;
        ApplyControlRegions(source, force: true);
    }

    /// <summary>Left edge of the controls in DIPs: the caption (drag) region stops there.</summary>
    private double WindowControlsLeft =>
        MinimizeHit.ActualWidth > 0
            ? MinimizeHit.TransformToVisual(RootGrid).TransformPoint(new Windows.Foundation.Point(0, 0)).X
            : RootGrid.ActualWidth;

    private void UpdateWindowControls()
    {
        var colors = JarvisPalette.Current;
        var highContrast = JarvisPalette.Appearance == JarvisAppearance.HighContrast;
        var lit = _windowFocused || _hoveredControl is not null;
        var glyphs = lit && (_hoveredControl is not null || highContrast);
        var maximized = (_appWindow.Presenter as OverlappedPresenter)?.State == OverlappedPresenterState.Maximized;

        void Style(Ellipse dot, Ellipse press, UIElement glyph, Windows.UI.Color color, NonClientRegionKind kind)
        {
            dot.Fill = new SolidColorBrush(lit ? color : colors.WindowControlInactive);
            dot.Stroke = highContrast ? new SolidColorBrush(colors.Label) : null;
            dot.StrokeThickness = highContrast ? 1 : 0;
            press.Opacity = _pressedControl == kind ? 0.22 : 0;
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
