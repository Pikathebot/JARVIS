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
    // iOS 26 at our scale (28pt track -> 32 DIP): puck 36.5x24pt -> 13.7 radius, 2.5pt inset ->
    // 2.85, 21pt travel -> 24; the track then comes out at iOS's 62.5pt (71.4 DIP).
    private const float ThumbRadius = 13.7f;
    private const float ThumbInset = 2.85f;

    /// <summary>The toggle's material, shared by every instance and live-tunable from the
    /// overlay's tuning sliders (call <see cref="MaterialChanged"/> after editing). Modeled on
    /// the iOS 26 switch: at rest nothing is glass -- an opaque capsule track (system green
    /// #34C759 on, light grey off) and an opaque white thumb with a soft shadow; while pressed
    /// the thumb lifts into a glass lens: grows past the track, turns transparent, elongates
    /// along its travel and refracts the track with a chromatic fringe at the rim.</summary>
    public static class Material
    {
        // Track (always opaque).
        public static Vector3 OnColor => JarvisPalette.ToVector3(JarvisPalette.Current.ToggleOn);
        /// <summary>toggle-off, flattened over glass-tint (the track paints opaque).</summary>
        public static Vector3 OffColor => JarvisPalette.Over(JarvisPalette.Current.ToggleOff, JarvisPalette.Current.GlassTint);
        public static float TrackBezel = 4f;
        public static float TrackRefraction = 2f;
        public static float TrackSpecular = 0.25f;

        // Thumb at rest: iOS 26's puck is a stadium 1.52x as wide as tall -- 73x48 px (36.5x24pt)
        // in a 125x56 px track, measured at the colour midpoint over 36 resting frames of the iPad
        // recording (t=70.0/72.45/75.5). The older 1.59 came from a brightness threshold that
        // shaved the puck's top and bottom edges.
        public static float RestAspect = 1.52f;
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
        public static float ToggleLiftScale = 1.65f;
        public static float LiftTint = 0.16f;
        /// <summary>Toggle thumb's lifted tint -- nearly clear, since over an opaque track any
        /// white wash just reads as a paler puck. The recording measures ~6%.</summary>
        public static float ToggleLiftTint = 0.06f;
        public static float LiftSpecular = 1.1f;
        public static float LiftShadow = 0.30f;
        public static float LiftShadowRadius = 12f;
        // Refraction is sized so the outer edge pulls the rail's edge just to the lens edge
        // (half-height minus rail half-thickness); more than that wraps the rail into a loop
        // inside the lens that reads as a second outline.
        public static float LiftBezelFraction = 0.48f;
        // iOS 26's pressed switch thumb, tracked frame by frame through three press-drag-release
        // cycles of the iPad recording (t=65-75, 60 fps): held still it is a clear 116x79 px
        // stadium -- 1.41x the 56 px track, aspect 1.47 -- overhanging the track top and bottom.
        // ToggleLiftScale 1.65 gives that height (27.4 x 1.65 / 32 = 1.41). No magnification -- content passes through the centre
        // undistorted -- and no colour fringe (R-B within +/-2); all the bending lives in the
        // rim, which bends OUTWARD over ~0.45 of the half-height, showing the dark background
        // beyond the track edge as a band just inside the rim. (The earlier 1.83 aspect and the
        // fringe were measured off the *slider*, which is 1.79 lifted -- not the switch.)
        public static float ToggleLiftBezelFraction = 0.46f;
        /// <summary>Negative = outward bend at the rim.</summary>
        public static float ToggleLiftRefraction = -9f;
        public static float ToggleLiftMagnify = 0f;
        /// <summary>Rim colour fringe while lifted -- none on the switch in the recording.</summary>
        public static float ToggleLiftChromatic = 0f;
        /// <summary>Peak frost (blur px) mid-way through the lift transition: on release the lens
        /// goes glass -> milky frosted -> opaque white over ~150ms rather than straight to white.
        /// Shaped as 4m(1-m) so it is zero at both rest and full lift.</summary>
        public static float ToggleLiftFrost = 5f;
        /// <summary>Lifted thumb width / height.</summary>
        public static float ToggleLiftAspect = 1.47f;
        public static float LiftRefraction = 14.8f;
        public static float LiftChromatic = 0.04f;
        public static float LiftBlur = 0f;

        public static float ShadowOffsetY = 1.5f;
        /// <summary>Debug: holds every thumb at least this far into its lifted state (kube.io's
        /// "Force active"), so the lens can be inspected without holding the pointer down.</summary>
        public static float ForceLift = 0f;
        /// <summary>Bright ~1px ring inside a lifted lens's rim (shared by the thumb, slider
        /// thumb and segmented pill); invisible on the opaque resting thumb anyway.</summary>
        public static float LiftEdgeRing = 0.22f;
        /// <summary>Exit-side sheen weight for every control's rim (see GlassShape.Params3.z).</summary>
        public static float SecondLight = 0.5f;

        /// <summary>Aspect gain per unit of travel velocity (travels/s) while the lens moves. The
        /// recording: +1.7% per 100 px/s of a 42 px travel = 0.7% per travel/s, and the lens gets
        /// shorter as well as longer (116x79 still -> 120x75 at ~400 px/s): see StretchWidthShare.</summary>
        public static float ThumbStretch = 0.007f;
        /// <summary>Share of the stretch taken as extra width; the rest comes off the height.</summary>
        public static float StretchWidthShare = 0.4f;
        /// <summary>How far past an end the thumb can be dragged, in travels: the recording's lens
        /// reached 24-25 px (0.58 of its 42 px travel) beyond either end and sprang back on release.
        /// A rubber band with this limit follows the finger 1:1 at first, then stiffens.</summary>
        public static float OverdragLimit = 0.75f;
    }

    private static readonly List<WeakReference<GlassToggle>> Instances = new();

    /// <summary>Re-publish every live toggle after a tuning change.</summary>
    public static void MaterialChanged()
    {
        foreach (var weak in Instances)
        {
            if (weak.TryGetTarget(out var toggle)) toggle.ApplySize();
        }
        GlassSlider.RepublishAll();
        GlassButton.RepublishAll();
        GlassTextField.RepublishAll();
        GlassSegmented.RepublishAll();
    }

    // Springs, fitted to the recording's switch (60 fps, three press-drag-release cycles, damped
    // step response per channel; residuals < 1 px). None of them visibly overshoots.
    //  travel      settle to an end after release: w 20 rad/s, zeta 1.07 (10-90% ~180 ms)
    //  lift up     press, puck -> lens:            w 35 rad/s, zeta 0.8  (10-90% ~70 ms)
    //  lift down   release, lens -> puck:          w 19 rad/s, zeta 1.1  (10-90% ~215 ms)
    // Pressing is twice as quick as letting go. (The old single spring, 520/30, was zeta 0.66:
    // a ~6% bounce the recording does not have.)
    private const float TravelStiffness = 400f, TravelDamping = 43f;
    /// <summary>Press spring (puck -> lens). Public: the slider's lift measured the same, and the
    /// segmented puck borrows it too (no segmented control in the recording to measure).</summary>
    public const float LiftUpStiffness = 1200f, LiftUpDamping = 55f;
    /// <summary>Release spring (lens -> puck); public for the segmented puck, see LiftUpStiffness.</summary>
    public const float LiftDownStiffness = 350f, LiftDownDamping = 41f;

    /// <summary>The press spring the other glass controls borrow for their own lift/focus until
    /// they are measured themselves (the old shared 520/30 until then).</summary>
    public const float SpringStiffness = 520f;
    public const float SpringDamping = 30f;

    private float _travel;        // current position 0..1
    private float _travelVelocity;
    private float _lift;          // 0 = resting opaque puck, 1 = lifted glass lens
    private float _liftVelocity;
    private bool _pressed;
    private bool _liftLatched;
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

    public static readonly DependencyProperty SwitchScaleProperty = DependencyProperty.Register(
        nameof(SwitchScale), typeof(double), typeof(GlassToggle), new PropertyMetadata(1.0, (d, _) => ((GlassToggle)d).ApplySize()));

    /// <summary>Uniform size of the whole switch; 1 = iOS 26's 28pt switch at our scale (71.4x32
    /// DIP). Everything -- track, puck, travel, bezels, shadows -- scales together, so the
    /// measured proportions hold at any size (the header uses a smaller one).</summary>
    public double SwitchScale
    {
        get => (double)GetValue(SwitchScaleProperty);
        set => SetValue(SwitchScaleProperty, value);
    }

    private float Size => (float)Math.Max(0.1, SwitchScale);

    private void ApplySize()
    {
        Width = TrackWidth * Size;
        Height = TrackHeight * Size;
        PublishShapes();
    }

    public event RoutedEventHandler? Toggled;

    public GlassToggle()
    {
        InitializeComponent();
        ApplySize();
        _travel = IsOn ? 1f : 0f;
        Instances.Add(new WeakReference<GlassToggle>(this));

        Loaded += (_, _) => { PublishShapes(); StartAnimating(); GlassScroll.Track(this, PublishShapes); };
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
    // it is on. The thumb moves *relative* to where the pointer started, not to wherever the
    // pointer is: grabbing the thumb by its edge must not make it jump, and nudging it further
    // toward the side it already sits on must leave it there. A press that never moved is a tap
    // and flips the state; any real movement is a drag and the state is whatever side wins.
    private const float DragThresholdDip = 2f;
    /// <summary>How far the pointer must get from where it went down before a release counts as
    /// a drag rather than a tap. Separate from <see cref="DragThresholdDip"/>, which only decides
    /// when the thumb starts following: a click on a touchpad or a finger tap wobbles a few DIPs,
    /// and when that wobble was enough to turn the tap into a "drag" that went nowhere, the
    /// release settled back where it started and the tap was simply lost. Judged on the
    /// *furthest* the pointer got, not where it ended, so a quick out-and-back waggle is still
    /// a drag.</summary>
    private const float TapSlopDip = 6f;
    private float _maxDragDistance;
    /// <summary>How far ahead of itself a released thumb is thrown when deciding which side it
    /// landed on, so a flick that was let go just short of an end still lands at that end.</summary>
    private const float ReleaseProjectionSeconds = 0.08f;
    private float _dragStartX;
    private float _dragStartTravel;
    private bool _dragging;
    private float _dragTarget;

    private void OnPointerPressed(object sender, PointerRoutedEventArgs e)
    {
        _pressed = true;
        _liftLatched = true;
        _dragging = false;
        _maxDragDistance = 0f;
        _dragStartX = (float)e.GetCurrentPoint(this).Position.X;
        _dragStartTravel = IsOn ? 1f : 0f;
        _dragTarget = _dragStartTravel;
        CapturePointer(e.Pointer);
        StartAnimating();
        e.Handled = true;
    }

    private void OnPointerMoved(object sender, PointerRoutedEventArgs e)
    {
        if (!_pressed) return;
        var x = (float)e.GetCurrentPoint(this).Position.X;
        var delta = x - _dragStartX;
        _maxDragDistance = Math.Max(_maxDragDistance, Math.Abs(delta));
        if (!_dragging && Math.Abs(delta) < DragThresholdDip) return;
        _dragging = true;

        // One full Travel of pointer movement moves the thumb from one end to the other; past an
        // end it keeps following on a rubber band (iOS lets the lens overrun and springs it back).
        var travel = Math.Max(1f, Travel * Size);
        _dragTarget = RubberBand(_dragStartTravel + delta / travel);
        e.Handled = true;
    }

    /// <summary>0..1 passes through; beyond either end the excess e becomes L*e/(L+e) -- slope 1
    /// at the end, never more than L past it.</summary>
    private static float RubberBand(float raw)
    {
        var limit = Math.Max(0.001f, Material.OverdragLimit);
        if (raw > 1f) { var e = raw - 1f; return 1f + limit * e / (limit + e); }
        if (raw < 0f) { var e = -raw; return -limit * e / (limit + e); }
        return raw;
    }

    private void OnPointerReleased(object sender, PointerRoutedEventArgs e)
    {
        if (_pressed)
        {
            // Read the drag state BEFORE releasing capture: ReleasePointerCapture raises
            // PointerCaptureLost synchronously, and that handler clears _dragging -- so a
            // release that read it afterwards always saw false, took the tap branch, and
            // flipped the state whatever the drag had done. A drag that crossed sides
            // looked right by accident; one that ended on the side it started from did not.
            var wasDragging = _dragging && _maxDragDistance >= TapSlopDip;
            _pressed = false;
            _dragging = false;
            ReleasePointerCapture(e.Pointer);
            if (wasDragging)
            {
                // Settle to the nearer side -- judged by where the thumb actually IS, plus where
                // it is heading, not by the raw pointer target. The thumb springs *after* the
                // pointer, so in a quick left-right waggle the pointer can already be back across
                // the middle while the puck the user is watching is still at the far end; deciding
                // on _dragTarget flipped a switch that had visibly been let go on its own side.
                // A drag that ends where it began still keeps the current state: the thumb is
                // there and not moving, so the projection lands on the same side.
                var projected = Math.Clamp(_travel + _travelVelocity * ReleaseProjectionSeconds, 0f, 1f);
                var on = projected >= 0.5f;
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
        // A press latches the lift until the lens is (nearly) fully up: a tap releases within a
        // frame or two, and without the latch the thumb would cross as an opaque puck. Once up,
        // the lens drops as soon as the pointer is gone -- WHILE the thumb is still travelling
        // back, as the recording shows (height and position settle together after a release),
        // not after it has landed.
        if (_liftLatched && !_pressed && _lift >= 0.9f) _liftLatched = false;
        var liftTarget = _pressed || _liftLatched ? 1f : 0f;

        Spring(ref _travel, ref _travelVelocity, travelTarget, dt, TravelStiffness, TravelDamping);
        if (liftTarget > _lift)
            Spring(ref _lift, ref _liftVelocity, liftTarget, dt, LiftUpStiffness, LiftUpDamping);
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

    /// <summary>Semi-implicit Euler in steps of at most 1/240 s: the lift spring is stiff enough
    /// that one 50 ms step after a dropped frame (damping x dt > 2) would ring instead of settle.</summary>
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
            return; // not in the tree yet
        }
        if (bounds.Width <= 0 || bounds.Height <= 0 || GlassSlab.IsCollapsedInTree(this)) { _scene.Remove(this); return; } // collapsed (or in a collapsed parent): take the glass with it
        var clip = GlassSlab.ClipFor(this, scale);

        var trackCenter = new Vector2((float)(bounds.X + bounds.Width * 0.5), (float)(bounds.Y + bounds.Height * 0.5)) * scale;
        scale *= Size; // from here on, sizes: the whole switch scales together
        var trackHalf = new Vector2(TrackWidth * 0.5f, TrackHeight * 0.5f) * scale;
        var trackRadius = TrackHeight * 0.5f * scale;

        var travelPx = Travel * scale;
        var thumbX = trackCenter.X - travelPx * 0.5f + _travel * travelPx;
        var thumbCenter = new Vector2(thumbX, trackCenter.Y);

        var m = Math.Max(_lift, Material.ForceLift); // rest -> lift blend
        var thumbRadius = ThumbRadius * (Material.RestScale + (Material.ToggleLiftScale - Material.RestScale) * m) * scale;

        // Apple's lifted thumb elongates along its direction of travel while moving -- longer AND
        // a little shorter, roughly keeping its area -- and relaxes as it settles; driven straight
        // off the spring's velocity.
        var stretch = Math.Min(0.25f, Math.Abs(_travelVelocity) * Material.ThumbStretch) * m;
        var aspect = Material.RestAspect + (Material.ToggleLiftAspect - Material.RestAspect) * m;
        var widthGain = 1f + stretch * Material.StretchWidthShare;
        var heightGain = 1f - stretch * (1f - Material.StretchWidthShare);
        thumbRadius *= heightGain;
        var thumbHalf = new Vector2(thumbRadius / heightGain * aspect * widthGain, thumbRadius);

        // Overdrag takes _travel past 0..1; the track colour stops at the ends.
        var trackColor = Vector3.Lerp(Material.OffColor, Material.OnColor, Math.Clamp(_travel, 0f, 1f));

        _scene.Publish(this,
            GlassShape.Create(trackCenter, trackHalf, trackRadius, Material.TrackBezel * scale, GlassBezelProfile.Lip,
                Material.TrackRefraction * scale, Material.TrackSpecular, layer: baseLayer, trackColor, 1f, clip: clip),
            GlassShape.Create(thumbCenter, thumbHalf, thumbRadius, thumbRadius * Material.ToggleLiftBezelFraction, GlassBezelProfile.Lens,
                refractionScale: Material.ToggleLiftRefraction * m * scale,
                specularIntensity: Material.RestSpecular + (Material.LiftSpecular - Material.RestSpecular) * m,
                layer: baseLayer + 1,
                tintColor: Vector3.One,
                tintAmount: Material.RestTint + (Material.ToggleLiftTint - Material.RestTint) * m,
                blurRadius: (Material.LiftBlur * m + Material.ToggleLiftFrost * 4f * m * (1f - m)) * scale,
                chromatic: Material.ToggleLiftChromatic * m,
                shadowStrength: Material.RestShadow + (Material.LiftShadow - Material.RestShadow) * m,
                shadowRadius: (Material.RestShadowRadius + (Material.LiftShadowRadius - Material.RestShadowRadius) * m) * scale,
                shadowOffsetY: Material.ShadowOffsetY * scale,
                magnify: Material.ToggleLiftMagnify * m,
                edgeRing: Material.LiftEdgeRing * m, clip: clip, secondLight: Material.SecondLight));
    }
}
