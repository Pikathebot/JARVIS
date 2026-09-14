using System.Numerics;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Media;

namespace Jarvis_Glass;

/// <summary>
/// kube.io's liquid-glass switch: a pill track with the lip bezel profile, and a thumb that is a
/// second, stronger lens sitting one layer above it -- so the thumb refracts the already-refracted
/// track, and the pair refracts the panel beneath. This control owns the interaction (toggle,
/// press, spring animation) and publishes its two shapes to <see cref="GlassScene"/> in
/// window pixel space every animation tick; GlassRenderer draws them. The XAML tree is only a
/// transparent hit-target.
///
/// The overlay window is pixel-locked to the glass window's rect, so XAML DIPs relative to the
/// overlay's root times RasterizationScale are exactly glass-window pixels.
/// </summary>
public sealed partial class GlassToggle : UserControl
{
    // Geometry in DIPs (kube.io's ~32px-tall switch); pixel values come from RasterizationScale.
    // Track width follows the puck: inset + resting puck width + travel + puck width + inset,
    // so the track always "fits" the puck at both ends whatever aspect it is given.
    private const float Travel = 24f;
    private static float ThumbRestHalfWidth => ThumbRadius * Material.RestAspect;
    private static float TrackWidth => 2f * (ThumbInset + ThumbRestHalfWidth) + Travel;
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

        // Thumb at rest: iOS 26's puck is a stadium 1.59x as wide as tall (measured off an iPad
        // screen recording: 36.5x23pt in a 62.5x28pt track, 2.5pt inset, 21pt travel).
        public static float RestAspect = 1.59f;
        public static float RestScale = 1.0f;
        public static float RestTint = 1.0f;       // opaque white
        public static float RestSpecular = 0.2f;
        public static float RestShadow = 0.28f;
        public static float RestShadowRadius = 6f;

        // Thumb lifted (pressed).
        // Matched against kube.io's slider in its forced-active state: a wide bezel that pulls
        // the rail right up into the lens's edge, a crisp (unblurred) lightly-whitened interior,
        // a thin bright outline and a blue-ish chromatic fringe.
        public static float LiftScale = 1.35f;
        /// <summary>The toggle thumb's own lift scale. The slider's thumb lifts over a thin rail
        /// and shows the card through it; the toggle's sits inside an opaque track, so at the
        /// slider's 1.35 it only shows refracted track and never reads as clear glass -- it has to
        /// overhang the track to show what is behind.</summary>
        public static float ToggleLiftScale = 1.46f;
        public static float LiftTint = 0.16f;
        /// <summary>Toggle thumb's lifted tint -- fully clear, since over an opaque track any
        /// white wash just reads as a paler puck.</summary>
        public static float ToggleLiftTint = 0.08f;
        public static float LiftSpecular = 1.1f;
        public static float LiftShadow = 0.30f;
        public static float LiftShadowRadius = 12f;
        // Refraction is sized so the outer edge pulls the rail's edge just to the lens edge
        // (half-height minus rail half-thickness); more than that wraps the rail into a loop
        // inside the lens that reads as a second outline.
        public static float LiftBezelFraction = 0.48f;
        // iOS 26's pressed switch thumb, measured frame-by-frame off the recording: a clear
        // 63x34.5pt stadium (1.46x the rest height, aspect 1.83) overhanging the 28pt track top
        // and bottom and hanging past its end. Two optical effects: the whole lens is a mild
        // magnifier (so the track colour fills it right out to the caps), and the rim bends
        // OUTWARD over ~0.46 of the half-height, which shows the dark background beyond the
        // track edge as a band just inside the rim -- thick at top/bottom, thin at the caps.
        public static float ToggleLiftBezelFraction = 0.46f;
        /// <summary>Negative = outward bend at the rim.</summary>
        public static float ToggleLiftRefraction = -9f;
        public static float ToggleLiftMagnify = 0.2f;
        /// <summary>Rim colour fringe while lifted -- clearly visible in the recording (orange
        /// one side, blue the other), far stronger than the slider's.</summary>
        public static float ToggleLiftChromatic = 0.12f;
        /// <summary>Peak frost (blur px) mid-way through the lift transition: on release the lens
        /// goes glass -> milky frosted -> opaque white over ~150ms rather than straight to white.
        /// Shaped as 4m(1-m) so it is zero at both rest and full lift.</summary>
        public static float ToggleLiftFrost = 5f;
        /// <summary>Lifted thumb width / height.</summary>
        public static float ToggleLiftAspect = 1.83f;
        public static float LiftRefraction = 14.8f;
        public static float LiftChromatic = 0.04f;
        public static float LiftBlur = 0f;

        public static float ShadowOffsetY = 1.5f;
        /// <summary>Debug: holds every thumb at least this far into its lifted state (kube.io's
        /// "Force active"), so the lens can be inspected without holding the pointer down.</summary>
        public static float ForceLift = 0f;
        /// <summary>How much the thumb elongates along its travel per unit of velocity.</summary>
        /// <summary>Bright ~1px ring inside a lifted lens's rim (shared by the thumb, slider
        /// thumb and segmented pill); invisible on the opaque resting thumb anyway.</summary>
        public static float LiftEdgeRing = 0.22f;
        /// <summary>Exit-side sheen weight for every control's rim (see GlassShape.Params3.z).</summary>
        public static float SecondLight = 0.5f;

        public static float ThumbStretch = 0.012f; // ~10% at the recording's ~10 travel/s drags
    }

    private static readonly List<WeakReference<GlassToggle>> Instances = new();

    /// <summary>Re-publish every live toggle after a tuning change.</summary>
    public static void MaterialChanged()
    {
        foreach (var weak in Instances)
        {
            if (weak.TryGetTarget(out var toggle)) { toggle.Width = TrackWidth; toggle.PublishShapes(); }
        }
        GlassSlider.RepublishAll();
        GlassButton.RepublishAll();
        GlassTextField.RepublishAll();
        GlassSegmented.RepublishAll();
    }

    // Spring for the thumb's travel (0 = off, 1 = on) and its press scale.
    public const float SpringStiffness = 520f;
    public const float SpringDamping = 30f; // release settles in ~270ms with a small overshoot (recording)
    private const float Stiffness = SpringStiffness;
    private const float Damping = SpringDamping;

    private float _travel;        // current position 0..1
    private float _travelVelocity;
    private float _lift;          // 0 = resting opaque puck, 1 = lifted glass lens
    private float _liftVelocity;
    private bool _pressed;
    private GlassScene? _scene;
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
        Width = TrackWidth;
        Height = TrackHeight;
        _travel = IsOn ? 1f : 0f;
        Instances.Add(new WeakReference<GlassToggle>(this));

        Loaded += (_, _) => { PublishShapes(); StartAnimating(); };
        Unloaded += (_, _) => { StopAnimating(); _scene?.Remove(this); _scene = null; };
        LayoutUpdated += (_, _) => PublishShapes();

        PointerPressed += OnPointerPressed;
        PointerMoved += OnPointerMoved;
        PointerReleased += OnPointerReleased;
        PointerCanceled += (_, _) => { _pressed = false; _dragging = false; StartAnimating(); };
        PointerCaptureLost += (_, _) => { _pressed = false; _dragging = false; StartAnimating(); };
    }

    private void OnIsOnChanged()
    {
        Toggled?.Invoke(this, new RoutedEventArgs());
        StartAnimating();
    }

    // Drag, as on iOS: while pressed the thumb follows the pointer along the track (springing
    // after it, so it still feels like the same object), and release snaps it to whichever side
    // it is on. A tap that never moved far enough to count as a drag just flips the state.
    private const float DragThresholdDip = 4f;
    private float _dragStartX;
    private bool _dragging;
    private float _dragTarget;

    private void OnPointerPressed(object sender, PointerRoutedEventArgs e)
    {
        _pressed = true;
        _dragging = false;
        _dragStartX = (float)e.GetCurrentPoint(this).Position.X;
        _dragTarget = IsOn ? 1f : 0f;
        CapturePointer(e.Pointer);
        StartAnimating();
        e.Handled = true;
    }

    private void OnPointerMoved(object sender, PointerRoutedEventArgs e)
    {
        if (!_pressed) return;
        var x = (float)e.GetCurrentPoint(this).Position.X;
        if (!_dragging && Math.Abs(x - _dragStartX) < DragThresholdDip) return;
        _dragging = true;

        // Map the pointer to the thumb-center travel range (same geometry as PublishShapes).
        var halfTravel = Travel * 0.5f;
        var centerX = (float)ActualWidth * 0.5f;
        _dragTarget = Math.Clamp((x - (centerX - halfTravel)) / (2f * halfTravel), 0f, 1f);
        e.Handled = true;
    }

    private void OnPointerReleased(object sender, PointerRoutedEventArgs e)
    {
        if (_pressed)
        {
            _pressed = false;
            ReleasePointerCapture(e.Pointer);
            var wasDragging = _dragging;
            _dragging = false;
            if (wasDragging)
            {
                var on = _dragTarget >= 0.5f;
                if (on == IsOn) StartAnimating(); // no state change; still need to settle the thumb
                IsOn = on;
            }
            else
            {
                IsOn = !IsOn;
            }
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

        var travelTarget = _dragging ? _dragTarget : (IsOn ? 1f : 0f);
        // Stay lifted for the whole slide, not just while the pointer is down: a tap releases
        // within a frame or two, so a press-only lift never gets past a few percent and the thumb
        // crosses as an opaque puck. iOS keeps the lens up until the thumb has landed.
        var travelling = Math.Abs(_travel - travelTarget) > 0.03f || Math.Abs(_travelVelocity) > 0.5f;
        var liftTarget = _pressed || travelling ? 1f : 0f;

        Spring(ref _travel, ref _travelVelocity, travelTarget, dt);
        Spring(ref _lift, ref _liftVelocity, liftTarget, dt);

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

    private static void Spring(ref float value, ref float velocity, float target, float dt)
    {
        var accel = (target - value) * Stiffness - velocity * Damping;
        velocity += accel * dt;
        value += velocity * dt;
    }

    private void PublishShapes()
    {
        if (XamlRoot is null || !IsLoaded) return;
        _scene ??= GlassScene.Find(this);
        if (_scene is null) return;

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

        var travelPx = Travel * scale;
        var thumbX = trackCenter.X - travelPx * 0.5f + _travel * travelPx;
        var thumbCenter = new Vector2(thumbX, trackCenter.Y);

        var m = Math.Max(_lift, Material.ForceLift); // rest -> lift blend
        var thumbRadius = ThumbRadius * (Material.RestScale + (Material.ToggleLiftScale - Material.RestScale) * m) * scale;

        // Apple's lifted thumb elongates along its direction of travel while moving and relaxes
        // back to a circle as it settles -- driven straight off the spring's velocity.
        var stretch = Math.Min(0.25f, Math.Abs(_travelVelocity) * Material.ThumbStretch) * m;
        var aspect = Material.RestAspect + (Material.ToggleLiftAspect - Material.RestAspect) * m;
        var thumbHalf = new Vector2(thumbRadius * aspect * (1f + stretch), thumbRadius);

        var trackColor = Vector3.Lerp(Material.OffColor, Material.OnColor, _travel);

        _scene.Publish(this,
            GlassShape.Create(trackCenter, trackHalf, trackRadius, Material.TrackBezel * scale, GlassBezelProfile.Lip,
                Material.TrackRefraction * scale, Material.TrackSpecular, layer: 2, trackColor, 1f),
            GlassShape.Create(thumbCenter, thumbHalf, thumbRadius, thumbRadius * Material.ToggleLiftBezelFraction, GlassBezelProfile.Lens,
                refractionScale: Material.ToggleLiftRefraction * m * scale,
                specularIntensity: Material.RestSpecular + (Material.LiftSpecular - Material.RestSpecular) * m,
                layer: 3,
                tintColor: Vector3.One,
                tintAmount: Material.RestTint + (Material.ToggleLiftTint - Material.RestTint) * m,
                blurRadius: (Material.LiftBlur * m + Material.ToggleLiftFrost * 4f * m * (1f - m)) * scale,
                chromatic: Material.ToggleLiftChromatic * m,
                shadowStrength: Material.RestShadow + (Material.LiftShadow - Material.RestShadow) * m,
                shadowRadius: (Material.RestShadowRadius + (Material.LiftShadowRadius - Material.RestShadowRadius) * m) * scale,
                shadowOffsetY: Material.ShadowOffsetY * scale,
                magnify: Material.ToggleLiftMagnify * m,
                edgeRing: Material.LiftEdgeRing * m, secondLight: Material.SecondLight));
    }
}
