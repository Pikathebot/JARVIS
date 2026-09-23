using System.Numerics;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Media;

namespace Jarvis_Glass;

/// <summary>
/// kube.io's liquid-glass button: a clear pill whose rim is a lens (the same empirical
/// <see cref="GlassBezelProfile.Lens"/> ramp as the lifted slider thumb, at a gentler
/// refraction), so the panel beneath bends visibly along its edge while the interior stays a
/// crisp, lightly-whitened window onto it. Hover brightens the interior; pressing lifts the slab
/// -- it grows, the rim bends harder with a chromatic fringe, the shadow deepens -- and release
/// springs it back and fires <see cref="Click"/>. Same publish-every-tick contract as
/// <see cref="GlassToggle"/>: one shape on layer 2, the XAML tree is a hit-target plus the label.
/// </summary>
public sealed partial class GlassButton : UserControl
{

    /// <summary>Shared, live-tunable material (see <see cref="GlassToggle.Material"/> for the
    /// convention). Rest numbers were picked so the rim bend reads on a frosted card without the
    /// label losing contrast; lift numbers are the toggle thumb's lens scaled down for a slab
    /// that is much wider than it is tall (the thumb's 16px bend on a 40px pill wraps the card
    /// edge into a loop).</summary>
    public static class Material
    {
        public static Vector3 ClearTint = Vector3.One;
        public static Vector3 AccentTint = new(0.039f, 0.518f, 1.0f); // system blue #0A84FF
        public static float AccentTintAmount = 0.55f;

        // Rim: lens ramp over a band that is this fraction of the corner radius.
        public static float BezelFraction = 0.55f;
        public static float RestRefraction = 7f;
        public static float RestSpecular = 0.7f;
        public static float RestEdgeRing = 0.18f;
        public static float LiftEdgeRing = 0.3f;
        public static float RestTint = 0.10f;
        public static float RestShadow = 0.18f;
        public static float RestShadowRadius = 8f;

        public static float HoverTintBoost = 0.08f;

        public static float LiftScale = 1.06f;
        public static float LiftRefraction = 13f;
        public static float LiftSpecular = 1.1f;
        /// <summary>Pressed white wash. iOS glass BRIGHTENS when pressed: the recording's Control
        /// Center button goes from luminance 41 to 90 (x2.2, a wash of roughly 5% -> 27%), so the
        /// pressed wash sits ~0.2 above rest. (It used to fall to 0.05 -- darker when pressed.)</summary>
        public static float LiftTint = 0.30f;
        public static float LiftChromatic = 0.03f;
        public static float LiftShadow = 0.32f;
        public static float LiftShadowRadius = 14f;

        public static float ShadowOffsetY = 2f;

        public static float LabelSize = 14f;
        public static Vector4 LabelColor = Vector4.One;
    }

    private static readonly List<WeakReference<GlassButton>> Instances = new();

    /// <summary>Re-publish every live button after a tuning change (called by GlassToggle.MaterialChanged).</summary>
    public static void RepublishAll()
    {
        foreach (var weak in Instances)
        {
            if (weak.TryGetTarget(out var button)) button.PublishShape();
        }
    }

    // Press and release, measured off a Control Center glass button in the iPad recording
    // (t=30.8-33.2, 60 fps, no dropped frames; radius fitted with a damped step response):
    //  press   66 -> 79.5 px pop, back to 76.5: w 25 rad/s, zeta 0.40 (~25% overshoot, 10-90% 58 ms)
    //  release 83 -> 62.5 px (below its 66 px rest) and back: w 17, zeta 0.45 (~21%, 10-90% 91 ms)
    // Unlike the switch and slider, iOS buttons DO bounce. The bounce shows in the slab's size
    // (the raw spring value drives the growth); material values use the value clamped to 0..1.
    // Size itself is not taken from the recording: that button is a round Control Center module
    // growing to 1.2x (1.27x held), far too much for a wide pill -- LiftScale stays ours.
    private const float PressStiffness = 630f, PressDamping = 20f;
    private const float ReleaseStiffness = 284f, ReleaseDamping = 15f;

    // Hover is a slower, softer spring than the press so it reads as a glow rather than a snap.
    private const float HoverStiffness = 260f;
    private const float HoverDamping = 22f;

    private float _lift, _liftVelocity;
    private float _hover, _hoverVelocity;
    private bool _pressed;
    private bool _pointerOver;
    private GlassScene? _scene;
    private bool _rendering;
    private long _lastTick;

    public static readonly DependencyProperty TextProperty = DependencyProperty.Register(
        nameof(Text), typeof(string), typeof(GlassButton), new PropertyMetadata("Button", (d, _) => ((GlassButton)d).PublishShape()));

    public string Text
    {
        get => (string)GetValue(TextProperty);
        set => SetValue(TextProperty, value);
    }

    public static readonly DependencyProperty IsAccentProperty = DependencyProperty.Register(
        nameof(IsAccent), typeof(bool), typeof(GlassButton), new PropertyMetadata(false, (d, _) => ((GlassButton)d).PublishShape()));

    /// <summary>Filled variant: an accent tint instead of clear glass. The rim lens is unchanged,
    /// so the edge still bends what is beneath it.</summary>
    public bool IsAccent
    {
        get => (bool)GetValue(IsAccentProperty);
        set => SetValue(IsAccentProperty, value);
    }

    public event RoutedEventHandler? Click;

    public GlassButton()
    {
        InitializeComponent();
        Instances.Add(new WeakReference<GlassButton>(this));

        Loaded += (_, _) => { PublishShape(); StartAnimating(); GlassScroll.Track(this, PublishShape); };
        Unloaded += (_, _) => { StopAnimating(); _scene?.Remove(this); _scene = null; };
        LayoutUpdated += (_, _) => PublishShape();

        PointerEntered += (_, _) => { _pointerOver = true; StartAnimating(); };
        PointerExited += (_, _) => { _pointerOver = false; StartAnimating(); };
        PointerPressed += OnPointerPressed;
        PointerReleased += OnPointerReleased;
        PointerCanceled += (_, _) => { _pressed = false; StartAnimating(); };
        PointerCaptureLost += (_, _) => { _pressed = false; StartAnimating(); };
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
            StartAnimating();

            // Only a release over the button counts -- dragging off and letting go cancels, as
            // every platform button does.
            var p = e.GetCurrentPoint(this).Position;
            if (p.X >= 0 && p.Y >= 0 && p.X < ActualWidth && p.Y < ActualHeight)
            {
                Click?.Invoke(this, new RoutedEventArgs());
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

    private void OnRendering(object? sender, object e)
    {
        var now = System.Diagnostics.Stopwatch.GetTimestamp();
        var dt = (float)System.Diagnostics.Stopwatch.GetElapsedTime(_lastTick, now).TotalSeconds;
        _lastTick = now;
        dt = Math.Clamp(dt, 0.001f, 0.05f);

        var liftTarget = _pressed ? 1f : 0f;
        var hoverTarget = _pointerOver || _pressed ? 1f : 0f;
        if (_pressed)
            Spring(ref _lift, ref _liftVelocity, liftTarget, dt, PressStiffness, PressDamping);
        else
            Spring(ref _lift, ref _liftVelocity, liftTarget, dt, ReleaseStiffness, ReleaseDamping);
        Spring(ref _hover, ref _hoverVelocity, hoverTarget, dt, HoverStiffness, HoverDamping);

        PublishShape();

        var settled = !_pressed
                   && Math.Abs(_lift - liftTarget) < 0.0005f && Math.Abs(_liftVelocity) < 0.01f
                   && Math.Abs(_hover - hoverTarget) < 0.0005f && Math.Abs(_hoverVelocity) < 0.01f;
        if (settled)
        {
            _lift = liftTarget; _liftVelocity = 0f;
            _hover = hoverTarget; _hoverVelocity = 0f;
            PublishShape();
            StopAnimating();
        }
    }

    /// <summary>Semi-implicit Euler in steps of at most 1/240 s, so a dropped frame (dt up to
    /// 50 ms) cannot make a spring ring harder than it should.</summary>
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

    private void PublishShape()
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

        // Raw spring value for the size, so the measured bounce overshoots on press and dips below
        // rest on release; clamped for everything else (a negative fringe or shadow is nonsense).
        var raw = Math.Max(_lift, GlassToggle.Material.ForceLift);
        var grow = 1f + (Material.LiftScale - 1f) * raw;
        var m = Math.Clamp(raw, 0f, 1f); // rest -> lift blend for the material

        var center = new Vector2((float)(bounds.X + bounds.Width * 0.5), (float)(bounds.Y + bounds.Height * 0.5)) * scale;
        var half = new Vector2((float)bounds.Width * 0.5f, (float)bounds.Height * 0.5f) * scale * grow;
        var radius = half.Y; // a true pill: corner radius is half the height

        var tintColor = IsAccent ? Material.AccentTint : Material.ClearTint;
        var restTint = IsAccent ? Material.AccentTintAmount : Material.RestTint;
        var liftTint = IsAccent ? Material.AccentTintAmount * 0.8f : Material.LiftTint;
        var tint = restTint + (liftTint - restTint) * m + Material.HoverTintBoost * _hover * (1f - m);

        _scene.Publish(this,
            GlassShape.Create(center, half, radius, radius * Material.BezelFraction, GlassBezelProfile.Lens,
                refractionScale: (Material.RestRefraction + (Material.LiftRefraction - Material.RestRefraction) * m) * scale,
                specularIntensity: Material.RestSpecular + (Material.LiftSpecular - Material.RestSpecular) * m,
                layer: baseLayer,
                tintColor: tintColor,
                tintAmount: Math.Clamp(tint, 0f, 1f),
                chromatic: Material.LiftChromatic * m,
                shadowStrength: Material.RestShadow + (Material.LiftShadow - Material.RestShadow) * m,
                shadowRadius: (Material.RestShadowRadius + (Material.LiftShadowRadius - Material.RestShadowRadius) * m) * scale,
                shadowOffsetY: Material.ShadowOffsetY * scale,
                edgeRing: Material.RestEdgeRing + (Material.LiftEdgeRing - Material.RestEdgeRing) * m,
                clip: clip, secondLight: GlassToggle.Material.SecondLight));

        // Caption on the button's own layer: composited after the slab, so it sits on the glass
        // (not bent by it) and scales with the lift like part of the slab.
        _scene.PublishText(this,
            new GlassText(Text ?? "", center, Material.LabelSize * scale * grow, GlassText.SemiBold, Material.LabelColor, Layer: baseLayer, Clip: clip));
    }
}
