using System.Numerics;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Media;

namespace Jarvis_GlassLab;

/// <summary>
/// The iOS-style slider, built from the same pieces as <see cref="GlassToggle"/>: an opaque
/// capsule rail (layer 2) with an accent-colored fill up to the thumb (layer 3), and the same
/// thumb (layer 4) -- an opaque white puck with a shadow at rest that lifts into a glass lens
/// while dragged, elongating along its travel. The thumb's material is deliberately shared with
/// the toggle's (<see cref="GlassToggle.Material"/>) so one tuning pass covers both.
///
/// Interaction: press anywhere on the control captures the pointer and the thumb springs toward
/// the pointer's position; dragging keeps retargeting it; release drops the lift. Value is 0..1.
/// </summary>
public sealed partial class GlassSlider : UserControl
{
    private const float TrackHeight = 32f;   // control height in DIPs; rail sits centered
    private const float ThumbRadius = 13.5f;
    private const float RailThickness = 6f;

    public static class Material
    {
        public static Vector3 RailColor = new(0.914f, 0.914f, 0.922f);
        public static Vector3 FillColor = new(0.039f, 0.518f, 1.0f); // system blue #0A84FF
        public static float RailSpecular = 0.2f;
        public static float RailBezel = 2.5f;
        /// <summary>Thumb width / height at rest: >1 is a lozenge along the rail, 1 a circle.</summary>
        public static float ThumbAspect = 1.4f;
    }

    private const float Stiffness = 700f;
    private const float Damping = 32f;

    private float _travel;
    private float _travelVelocity;
    private float _target;
    private float _lift;
    private float _liftVelocity;
    private bool _pressed;
    private bool _rendering;
    private long _lastTick;

    public static readonly DependencyProperty ValueProperty = DependencyProperty.Register(
        nameof(Value), typeof(double), typeof(GlassSlider), new PropertyMetadata(0.0, (d, _) => ((GlassSlider)d).OnValueChanged()));

    /// <summary>0..1.</summary>
    public double Value
    {
        get => (double)GetValue(ValueProperty);
        set => SetValue(ValueProperty, Math.Clamp(value, 0.0, 1.0));
    }

    public event RoutedEventHandler? ValueChanged;

    private static readonly List<WeakReference<GlassSlider>> Instances = new();

    /// <summary>Re-publish every live slider after a tuning change (called by GlassToggle.MaterialChanged).</summary>
    public static void RepublishAll()
    {
        foreach (var weak in Instances)
        {
            if (weak.TryGetTarget(out var slider)) slider.PublishShapes();
        }
    }

    public GlassSlider()
    {
        InitializeComponent();
        _travel = _target = (float)Value;
        Instances.Add(new WeakReference<GlassSlider>(this));

        Loaded += (_, _) => { PublishShapes(); StartAnimating(); };
        Unloaded += (_, _) => { StopAnimating(); GlassShapeRegistry.Remove(this); };
        LayoutUpdated += (_, _) => PublishShapes();

        PointerPressed += OnPointerPressed;
        PointerMoved += OnPointerMoved;
        PointerReleased += OnPointerReleased;
        PointerCanceled += (_, _) => { _pressed = false; StartAnimating(); };
        PointerCaptureLost += (_, _) => { _pressed = false; StartAnimating(); };
    }

    private void OnValueChanged()
    {
        _target = (float)Value;
        ValueChanged?.Invoke(this, new RoutedEventArgs());
        StartAnimating();
    }

    private float ValueFromPointer(PointerRoutedEventArgs e)
    {
        var x = e.GetCurrentPoint(this).Position.X;
        var halfW = ThumbRadius * Material.ThumbAspect;
        var usable = ActualWidth - 2 * halfW;
        return usable <= 0 ? 0f : (float)Math.Clamp((x - halfW) / usable, 0.0, 1.0);
    }

    private void OnPointerPressed(object sender, PointerRoutedEventArgs e)
    {
        _pressed = true;
        CapturePointer(e.Pointer);
        Value = ValueFromPointer(e);
        StartAnimating();
        e.Handled = true;
    }

    private void OnPointerMoved(object sender, PointerRoutedEventArgs e)
    {
        if (!_pressed) return;
        Value = ValueFromPointer(e);
        e.Handled = true;
    }

    private void OnPointerReleased(object sender, PointerRoutedEventArgs e)
    {
        if (_pressed)
        {
            _pressed = false;
            ReleasePointerCapture(e.Pointer);
            StartAnimating();
        }
        e.Handled = true;
    }

    private void StartAnimating()
    {
        if (_rendering) return;
        _rendering = true;
        _lastTick = System.Diagnostics.Stopwatch.GetTimestamp();
        CompositionTarget.Rendering += OnRendering;
    }

    private void StopAnimating()
    {
        if (!_rendering) return;
        _rendering = false;
        CompositionTarget.Rendering -= OnRendering;
    }

    private void OnRendering(object? sender, object e)
    {
        var now = System.Diagnostics.Stopwatch.GetTimestamp();
        var dt = (float)System.Diagnostics.Stopwatch.GetElapsedTime(_lastTick, now).TotalSeconds;
        _lastTick = now;
        dt = Math.Clamp(dt, 0.001f, 0.05f);

        var liftTarget = _pressed ? 1f : 0f;
        Spring(ref _travel, ref _travelVelocity, _target, dt, Stiffness, Damping);
        Spring(ref _lift, ref _liftVelocity, liftTarget, dt, GlassToggle.SpringStiffness, GlassToggle.SpringDamping);

        PublishShapes();

        var settled = !_pressed
                   && Math.Abs(_travel - _target) < 0.0005f && Math.Abs(_travelVelocity) < 0.01f
                   && Math.Abs(_lift - liftTarget) < 0.0005f && Math.Abs(_liftVelocity) < 0.01f;
        if (settled)
        {
            _travel = _target; _travelVelocity = 0f;
            _lift = liftTarget; _liftVelocity = 0f;
            PublishShapes();
            StopAnimating();
        }
    }

    private static void Spring(ref float value, ref float velocity, float target, float dt, float stiffness, float damping)
    {
        var accel = (target - value) * stiffness - velocity * damping;
        velocity += accel * dt;
        value += velocity * dt;
    }

    private void PublishShapes()
    {
        if (XamlRoot is null || !IsLoaded) return;

        float scale;
        Windows.Foundation.Rect bounds;
        try
        {
            scale = (float)XamlRoot.RasterizationScale;
            bounds = TransformToVisual(null).TransformBounds(new Windows.Foundation.Rect(0, 0, ActualWidth, ActualHeight));
        }
        catch
        {
            return;
        }
        if (bounds.Width <= 0 || bounds.Height <= 0) return;

        var centerY = (float)(bounds.Y + bounds.Height * 0.5) * scale;
        var left = (float)bounds.X * scale;
        var width = (float)bounds.Width * scale;

        // Rail spans the thumb's center travel plus half a thumb each side so the caps sit under
        // the thumb at both extremes, as on iOS.
        var thumbR = ThumbRadius * scale;
        var railHalfH = RailThickness * 0.5f * scale;
        var railCenter = new Vector2(left + width * 0.5f, centerY);
        var railHalf = new Vector2(width * 0.5f, railHalfH);

        var thumbHalfW = thumbR * Material.ThumbAspect;
        var travelPx = width - 2f * thumbHalfW;
        var thumbX = left + thumbHalfW + _travel * travelPx;

        // Fill: from the rail's left cap to a point that slides across the thumb with the value --
        // its left edge at 0, its right edge at 1. Ending at the thumb's center would show grey
        // rail beyond the blue through the lifted (transparent) thumb at 100%.
        var fillEnd = thumbX + (_travel - 0.5f) * 2f * thumbHalfW;
        var fillW = Math.Max(2f * railHalfH, fillEnd - left);
        var fillCenter = new Vector2(left + fillW * 0.5f, centerY);
        var fillHalf = new Vector2(fillW * 0.5f, railHalfH);
        // At 0% there is nothing to fill: the rail-height minimum above exists so the capsule
        // keeps its round cap while shrinking, but published at zero it left a blue dot under
        // the thumb's left edge.
        var hasFill = fillEnd - left > 0.5f;

        var m = Math.Max(_lift, GlassToggle.Material.ForceLift);
        var thumbRadius = ThumbRadius * (GlassToggle.Material.RestScale + (GlassToggle.Material.LiftScale - GlassToggle.Material.RestScale) * m) * scale;
        var stretch = Math.Min(0.25f, Math.Abs(_travelVelocity) * GlassToggle.Material.ThumbStretch) * m;
        var thumbHalf = new Vector2(thumbRadius * Material.ThumbAspect * (1f + stretch), thumbRadius);
        var thumbCenter = new Vector2(thumbX, centerY);

        var shapes = new List<GlassShape>(3)
        {
            GlassShape.Create(railCenter, railHalf, railHalfH, Material.RailBezel * scale, GlassBezelProfile.Lip,
                2f * scale, Material.RailSpecular, layer: 2, Material.RailColor, 1f),
        };
        if (hasFill)
        {
            shapes.Add(GlassShape.Create(fillCenter, fillHalf, railHalfH, Material.RailBezel * scale, GlassBezelProfile.Lip,
                2f * scale, Material.RailSpecular, layer: 3, Material.FillColor, 1f));
        }
        shapes.Add(GlassShape.Create(thumbCenter, thumbHalf, thumbRadius, thumbRadius * GlassToggle.Material.LiftBezelFraction, GlassBezelProfile.Lens,
                refractionScale: GlassToggle.Material.LiftRefraction * m * scale,
                specularIntensity: GlassToggle.Material.RestSpecular + (GlassToggle.Material.LiftSpecular - GlassToggle.Material.RestSpecular) * m,
                layer: 4,
                tintColor: Vector3.One,
                tintAmount: GlassToggle.Material.RestTint + (GlassToggle.Material.LiftTint - GlassToggle.Material.RestTint) * m,
                blurRadius: GlassToggle.Material.LiftBlur * m * scale,
                chromatic: GlassToggle.Material.LiftChromatic * m,
                shadowStrength: GlassToggle.Material.RestShadow + (GlassToggle.Material.LiftShadow - GlassToggle.Material.RestShadow) * m,
                shadowRadius: (GlassToggle.Material.RestShadowRadius + (GlassToggle.Material.LiftShadowRadius - GlassToggle.Material.RestShadowRadius) * m) * scale,
                shadowOffsetY: GlassToggle.Material.ShadowOffsetY * scale,
                edgeRing: GlassToggle.Material.LiftEdgeRing * m, secondLight: GlassToggle.Material.SecondLight));
        GlassShapeRegistry.Publish(this, shapes.ToArray());
    }
}
