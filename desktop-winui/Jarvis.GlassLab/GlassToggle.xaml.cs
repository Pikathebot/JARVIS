using System.Numerics;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Media;

namespace Jarvis_GlassLab;

/// <summary>
/// kube.io's liquid-glass switch: a pill track with the lip bezel profile, and a thumb that is a
/// second, stronger lens sitting one layer above it -- so the thumb refracts the already-refracted
/// track, and the pair refracts the panel beneath. This control owns the interaction (toggle,
/// press, spring animation) and publishes its two shapes to <see cref="GlassShapeRegistry"/> in
/// window pixel space every animation tick; GlassRenderer draws them. The XAML tree is only a
/// transparent hit-target.
///
/// The overlay window is pixel-locked to the glass window's rect, so XAML DIPs relative to the
/// overlay's root times RasterizationScale are exactly glass-window pixels.
/// </summary>
public sealed partial class GlassToggle : UserControl
{
    // Geometry in DIPs (kube.io's ~32px-tall switch); pixel values come from RasterizationScale.
    private const float TrackWidth = 56f;
    private const float TrackHeight = 32f;
    private const float ThumbRadius = 13.5f;
    private const float ThumbInset = 2.5f;

    /// <summary>The toggle's material, shared by every instance and live-tunable from the
    /// overlay's tuning sliders (call <see cref="MaterialChanged"/> after editing). Modeled on
    /// the iOS 26 switch: at rest nothing is glass -- an opaque capsule track (system green
    /// #34C759 on, light grey off) and an opaque white thumb with a soft shadow; while pressed
    /// the thumb lifts into a glass lens: grows past the track, turns transparent, elongates
    /// along its travel and refracts the track with a chromatic fringe at the rim.</summary>
    public static class Material
    {
        // Track (always opaque).
        public static Vector3 OnColor = new(0.204f, 0.780f, 0.349f);
        public static Vector3 OffColor = new(0.914f, 0.914f, 0.922f);
        public static float TrackBezel = 4f;
        public static float TrackRefraction = 2f;
        public static float TrackSpecular = 0.25f;

        // Thumb at rest.
        public static float RestScale = 1.0f;
        public static float RestTint = 1.0f;       // opaque white
        public static float RestSpecular = 0.2f;
        public static float RestShadow = 0.28f;
        public static float RestShadowRadius = 6f;

        // Thumb lifted (pressed).
        public static float LiftScale = 1.35f;
        public static float LiftTint = 0.10f;      // near-clear glass
        public static float LiftSpecular = 0.9f;
        public static float LiftShadow = 0.35f;
        public static float LiftShadowRadius = 12f;
        public static float LiftBezelFraction = 0.36f;
        public static float LiftRefraction = 16f;
        public static float LiftChromatic = 2.0f;
        public static float LiftBlur = 1.5f;

        public static float ShadowOffsetY = 1.5f;
        /// <summary>How much the thumb elongates along its travel per unit of velocity.</summary>
        public static float ThumbStretch = 0.06f;
    }

    private static readonly List<WeakReference<GlassToggle>> Instances = new();

    /// <summary>Re-publish every live toggle after a tuning change.</summary>
    public static void MaterialChanged()
    {
        foreach (var weak in Instances)
        {
            if (weak.TryGetTarget(out var toggle)) toggle.PublishShapes();
        }
    }

    // Spring for the thumb's travel (0 = off, 1 = on) and its press scale.
    private const float Stiffness = 520f;
    private const float Damping = 24f;

    private float _travel;        // current position 0..1
    private float _travelVelocity;
    private float _lift;          // 0 = resting opaque puck, 1 = lifted glass lens
    private float _liftVelocity;
    private bool _pressed;
    private bool _rendering;
    private long _lastTick;

    public static readonly DependencyProperty IsOnProperty = DependencyProperty.Register(
        nameof(IsOn), typeof(bool), typeof(GlassToggle), new PropertyMetadata(false, (d, _) => ((GlassToggle)d).OnIsOnChanged()));

    public bool IsOn
    {
        get => (bool)GetValue(IsOnProperty);
        set => SetValue(IsOnProperty, value);
    }

    public event RoutedEventHandler? Toggled;

    public GlassToggle()
    {
        InitializeComponent();
        _travel = IsOn ? 1f : 0f;
        Instances.Add(new WeakReference<GlassToggle>(this));

        Loaded += (_, _) => { PublishShapes(); StartAnimating(); };
        Unloaded += (_, _) => { StopAnimating(); GlassShapeRegistry.Remove(this); };
        LayoutUpdated += (_, _) => PublishShapes();

        PointerPressed += OnPointerPressed;
        PointerReleased += OnPointerReleased;
        PointerCanceled += (_, _) => { _pressed = false; StartAnimating(); };
        PointerCaptureLost += (_, _) => { _pressed = false; StartAnimating(); };
    }

    private void OnIsOnChanged()
    {
        Toggled?.Invoke(this, new RoutedEventArgs());
        StartAnimating();
    }

    private void OnPointerPressed(object sender, PointerRoutedEventArgs e)
    {
        _pressed = true;
        CapturePointer(e.Pointer);
        StartAnimating();
        e.Handled = true;
    }

    private void OnPointerReleased(object sender, PointerRoutedEventArgs e)
    {
        if (_pressed)
        {
            _pressed = false;
            ReleasePointerCapture(e.Pointer);
            IsOn = !IsOn;
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

    /// <summary>Semi-implicit spring integration per frame; unsubscribes itself once everything
    /// has settled so an idle toggle costs nothing.</summary>
    private void OnRendering(object? sender, object e)
    {
        var now = System.Diagnostics.Stopwatch.GetTimestamp();
        var dt = (float)System.Diagnostics.Stopwatch.GetElapsedTime(_lastTick, now).TotalSeconds;
        _lastTick = now;
        dt = Math.Clamp(dt, 0.001f, 0.05f);

        var travelTarget = IsOn ? 1f : 0f;
        var liftTarget = _pressed ? 1f : 0f;

        Spring(ref _travel, ref _travelVelocity, travelTarget, dt);
        Spring(ref _lift, ref _liftVelocity, liftTarget, dt);

        PublishShapes();

        var settled = Math.Abs(_travel - travelTarget) < 0.0005f && Math.Abs(_travelVelocity) < 0.01f
                   && Math.Abs(_lift - liftTarget) < 0.0005f && Math.Abs(_liftVelocity) < 0.01f;
        if (settled)
        {
            _travel = travelTarget; _travelVelocity = 0f;
            _lift = liftTarget; _liftVelocity = 0f;
            PublishShapes();
            StopAnimating();
        }
    }

    private static void Spring(ref float value, ref float velocity, float target, float dt)
    {
        var accel = (target - value) * Stiffness - velocity * Damping;
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
            return; // not in the tree yet
        }
        if (bounds.Width <= 0 || bounds.Height <= 0) return;

        var trackCenter = new Vector2((float)(bounds.X + bounds.Width * 0.5), (float)(bounds.Y + bounds.Height * 0.5)) * scale;
        var trackHalf = new Vector2(TrackWidth * 0.5f, TrackHeight * 0.5f) * scale;
        var trackRadius = TrackHeight * 0.5f * scale;

        var travelPx = (TrackWidth - 2f * (ThumbInset + ThumbRadius)) * scale;
        var thumbX = trackCenter.X - travelPx * 0.5f + _travel * travelPx;
        var thumbCenter = new Vector2(thumbX, trackCenter.Y);

        var m = _lift; // rest -> lift blend
        var thumbRadius = ThumbRadius * (Material.RestScale + (Material.LiftScale - Material.RestScale) * m) * scale;

        // Apple's lifted thumb elongates along its direction of travel while moving and relaxes
        // back to a circle as it settles -- driven straight off the spring's velocity.
        var stretch = Math.Min(0.45f, Math.Abs(_travelVelocity) * Material.ThumbStretch) * m;
        var thumbHalf = new Vector2(thumbRadius * (1f + stretch), thumbRadius);

        var trackColor = Vector3.Lerp(Material.OffColor, Material.OnColor, _travel);

        GlassShapeRegistry.Publish(this,
            GlassShape.Create(trackCenter, trackHalf, trackRadius, Material.TrackBezel * scale, GlassBezelProfile.Lip,
                Material.TrackRefraction * scale, Material.TrackSpecular, layer: 2, trackColor, 1f),
            GlassShape.Create(thumbCenter, thumbHalf, thumbRadius, thumbRadius * Material.LiftBezelFraction, GlassBezelProfile.Squircle,
                refractionScale: Material.LiftRefraction * m * scale,
                specularIntensity: Material.RestSpecular + (Material.LiftSpecular - Material.RestSpecular) * m,
                layer: 3,
                tintColor: Vector3.One,
                tintAmount: Material.RestTint + (Material.LiftTint - Material.RestTint) * m,
                blurRadius: Material.LiftBlur * m * scale,
                chromatic: Material.LiftChromatic * m * scale,
                shadowStrength: Material.RestShadow + (Material.LiftShadow - Material.RestShadow) * m,
                shadowRadius: (Material.RestShadowRadius + (Material.LiftShadowRadius - Material.RestShadowRadius) * m) * scale,
                shadowOffsetY: Material.ShadowOffsetY * scale));
    }
}
