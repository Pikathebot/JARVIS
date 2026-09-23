using System.Numerics;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Media;

namespace Jarvis_Glass;

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
    // iOS 26: a 6pt rail under a 24pt-tall thumb (12 / 48 px in the recording); at our 27 DIP thumb
    // that is 6.75.
    private const float RailThickness = 6.75f;

    public static class Material
    {
        public static Vector3 RailColor = new(0.914f, 0.914f, 0.922f);
        public static Vector3 FillColor = new(0.039f, 0.518f, 1.0f); // system blue #0A84FF
        public static float RailSpecular = 0.2f;
        public static float RailBezel = 2.5f;
        // Measured off the iPad recording's Liquid Glass slider (t=36.5-63, 60 fps, 570 resting
        // and ~450 lifted frames): resting thumb 74x48 px, lifted and held still 112x81 px.
        /// <summary>Thumb width / height at rest.</summary>
        public static float ThumbAspect = 1.54f;
        /// <summary>Lifted thumb width / height while held still (the lens is rounder than the
        /// puck; it only elongates while moving -- see StretchPerDipPerSecond).</summary>
        public static float LiftAspect = 1.38f;
        /// <summary>Lifted height / resting height (81 / 48 px). The toggle's lens has its own.</summary>
        public static float LiftScale = 1.69f;
        /// <summary>Aspect gain per DIP/s of thumb speed: the recording's lens gains ~1% per
        /// 100 px/s and reaches ~+42% at 3700 px/s (135x69 px against 112x81 still).</summary>
        public static float StretchPerDipPerSecond = 0.00018f;
        public static float StretchMax = 0.45f;
        /// <summary>Share of the stretch taken as extra width; the rest comes off the height.</summary>
        public static float StretchWidthShare = 0.6f;
        /// <summary>How far past either end the lens follows a drag (rubber band) before springing
        /// back on release: 24-29 px (~13pt) in the recording.</summary>
        public static float OverdragDip = 15f;
    }

    // Travel follows the pointer. The old 700/32 (zeta 0.6) overshot every stop; the recording's
    // thumb never overshoots, including the spring back from an overdrag, so same speed,
    // critically damped.
    private const float Stiffness = 700f;
    private const float Damping = 53f;
    // Release: lens -> puck, w 15 rad/s, zeta 0.8 in the recording (two clean fits). The press
    // itself matches the toggle's (48 -> 75 px in ~80 ms), so it borrows GlassToggle's lift-up.
    private const float LiftDownStiffness = 225f, LiftDownDamping = 24f;

    private float _overdrag;   // travel units past 0 or 1 while dragging beyond an end

    private float _travel;
    private float _travelVelocity;
    private float _target;
    private float _lift;
    private float _liftVelocity;
    private bool _pressed;
    private GlassScene? _scene;
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

        Loaded += (_, _) => { PublishShapes(); StartAnimating(); GlassScroll.Track(this, PublishShapes); };
        Unloaded += (_, _) => { StopAnimating(); _scene?.Remove(this); _scene = null; };
        LayoutUpdated += (_, _) => PublishShapes();

        PointerPressed += OnPointerPressed;
        PointerMoved += OnPointerMoved;
        PointerReleased += OnPointerReleased;
        PointerCanceled += (_, _) => { _pressed = false; _overdrag = 0f; StartAnimating(); };
        PointerCaptureLost += (_, _) => { _pressed = false; _overdrag = 0f; StartAnimating(); };
    }

    private void OnValueChanged()
    {
        _target = (float)Value;
        ValueChanged?.Invoke(this, new RoutedEventArgs());
        StartAnimating();
    }

    /// <summary>The pointer's position in travel units, NOT clamped: beyond 0..1 is overdrag.</summary>
    private float RawFromPointer(PointerRoutedEventArgs e)
    {
        var x = e.GetCurrentPoint(this).Position.X;
        var halfW = ThumbRadius * Material.ThumbAspect;
        var usable = ActualWidth - 2 * halfW;
        return usable <= 0 ? 0f : (float)((x - halfW) / usable);
    }

    /// <summary>Sets Value from the pointer and keeps any excess as a rubber-banded overdrag:
    /// L*e/(L+e) past the end, L = OverdragDip in travel units.</summary>
    private void TrackPointer(PointerRoutedEventArgs e)
    {
        var raw = RawFromPointer(e);
        var usableDip = ActualWidth - 2 * ThumbRadius * Material.ThumbAspect;
        var limit = usableDip > 0 ? (float)(Material.OverdragDip / usableDip) : 0f;
        var excess = raw > 1f ? raw - 1f : raw < 0f ? raw : 0f;
        var mag = Math.Abs(excess);
        _overdrag = limit <= 0 ? 0f : Math.Sign(excess) * limit * mag / (limit + mag);
        Value = Math.Clamp(raw, 0f, 1f);
        StartAnimating();
    }

    private void OnPointerPressed(object sender, PointerRoutedEventArgs e)
    {
        _pressed = true;
        CapturePointer(e.Pointer);
        TrackPointer(e);
        e.Handled = true;
    }

    private void OnPointerMoved(object sender, PointerRoutedEventArgs e)
    {
        if (!_pressed) return;
        TrackPointer(e);
        e.Handled = true;
    }

    private void OnPointerReleased(object sender, PointerRoutedEventArgs e)
    {
        if (_pressed)
        {
            _pressed = false;
            _overdrag = 0f; // spring back from past the end
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
        var travelTarget = _target + _overdrag;
        Spring(ref _travel, ref _travelVelocity, travelTarget, dt, Stiffness, Damping);
        if (liftTarget > _lift)
            Spring(ref _lift, ref _liftVelocity, liftTarget, dt, GlassToggle.LiftUpStiffness, GlassToggle.LiftUpDamping);
        else
            Spring(ref _lift, ref _liftVelocity, liftTarget, dt, LiftDownStiffness, LiftDownDamping);

        PublishShapes();

        var settled = !_pressed
                   && Math.Abs(_travel - travelTarget) < 0.0005f && Math.Abs(_travelVelocity) < 0.01f
                   && Math.Abs(_lift - liftTarget) < 0.0005f && Math.Abs(_liftVelocity) < 0.01f;
        if (settled)
        {
            _travel = travelTarget; _travelVelocity = 0f;
            _lift = liftTarget; _liftVelocity = 0f;
            PublishShapes();
            StopAnimating();
        }
    }

    /// <summary>Semi-implicit Euler in steps of at most 1/240 s, so a dropped frame (dt up to
    /// 50 ms) cannot make the stiffer springs ring.</summary>
    private static void Spring(ref float value, ref float velocity, float target, float dt, float stiffness, float damping)
    {
        const float MaxStep = 1f / 240f;
        var steps = Math.Max(1, (int)MathF.Ceiling(dt / MaxStep));
        var h = dt / steps;
        for (var i = 0; i < steps; i++)
        {
            var accel = (target - value) * stiffness - velocity * damping;
            velocity += accel * h;
            value += velocity * h;
        }
    }

    private void PublishShapes()
    {
        if (XamlRoot is null || !IsLoaded) return;
        _scene ??= GlassScene.Find(this);
        if (_scene is null) return;
        var baseLayer = GlassSlab.BaseLayerFor(this);

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
        if (bounds.Width <= 0 || bounds.Height <= 0 || GlassSlab.IsCollapsedInTree(this)) { _scene.Remove(this); return; } // collapsed (or in a collapsed parent): take the glass with it
        var clip = GlassSlab.ClipFor(this, scale);

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
        var fillTravel = Math.Clamp(_travel, 0f, 1f);
        var fillEnd = left + thumbHalfW + fillTravel * travelPx + (fillTravel - 0.5f) * 2f * thumbHalfW;
        var fillW = Math.Max(2f * railHalfH, fillEnd - left);
        var fillCenter = new Vector2(left + fillW * 0.5f, centerY);
        var fillHalf = new Vector2(fillW * 0.5f, railHalfH);
        // At 0% there is nothing to fill: the rail-height minimum above exists so the capsule
        // keeps its round cap while shrinking, but published at zero it left a blue dot under
        // the thumb's left edge.
        var hasFill = fillEnd - left > 0.5f;

        var m = Math.Max(_lift, GlassToggle.Material.ForceLift);
        var thumbRadius = ThumbRadius * (1f + (Material.LiftScale - 1f) * m) * scale;
        var aspect = Material.ThumbAspect + (Material.LiftAspect - Material.ThumbAspect) * m;
        // Speed in DIP/s: travel units/s x the thumb's travel span in DIPs.
        var speedDip = Math.Abs(_travelVelocity) * travelPx / scale;
        var stretch = Math.Min(Material.StretchMax, speedDip * Material.StretchPerDipPerSecond) * m;
        var widthGain = 1f + stretch * Material.StretchWidthShare;
        var heightGain = 1f - stretch * (1f - Material.StretchWidthShare);
        var thumbHalf = new Vector2(thumbRadius * aspect * widthGain, thumbRadius * heightGain);
        thumbRadius *= heightGain;
        var thumbCenter = new Vector2(thumbX, centerY);

        var shapes = new List<GlassShape>(3)
        {
            GlassShape.Create(railCenter, railHalf, railHalfH, Material.RailBezel * scale, GlassBezelProfile.Lip,
                2f * scale, Material.RailSpecular, layer: baseLayer, Material.RailColor, 1f, clip: clip),
        };
        if (hasFill)
        {
            shapes.Add(GlassShape.Create(fillCenter, fillHalf, railHalfH, Material.RailBezel * scale, GlassBezelProfile.Lip,
                2f * scale, Material.RailSpecular, layer: baseLayer + 1, Material.FillColor, 1f, clip: clip));
        }
        shapes.Add(GlassShape.Create(thumbCenter, thumbHalf, thumbRadius, thumbRadius * GlassToggle.Material.LiftBezelFraction, GlassBezelProfile.Lens,
                refractionScale: GlassToggle.Material.LiftRefraction * m * scale,
                specularIntensity: GlassToggle.Material.RestSpecular + (GlassToggle.Material.LiftSpecular - GlassToggle.Material.RestSpecular) * m,
                layer: baseLayer + 2,
                tintColor: Vector3.One,
                tintAmount: GlassToggle.Material.RestTint + (GlassToggle.Material.LiftTint - GlassToggle.Material.RestTint) * m,
                blurRadius: GlassToggle.Material.LiftBlur * m * scale,
                chromatic: GlassToggle.Material.LiftChromatic * m,
                shadowStrength: GlassToggle.Material.RestShadow + (GlassToggle.Material.LiftShadow - GlassToggle.Material.RestShadow) * m,
                shadowRadius: (GlassToggle.Material.RestShadowRadius + (GlassToggle.Material.LiftShadowRadius - GlassToggle.Material.RestShadowRadius) * m) * scale,
                shadowOffsetY: GlassToggle.Material.ShadowOffsetY * scale,
                edgeRing: GlassToggle.Material.LiftEdgeRing * m, clip: clip, secondLight: GlassToggle.Material.SecondLight));
        _scene.Publish(this, shapes.ToArray());
    }
}
