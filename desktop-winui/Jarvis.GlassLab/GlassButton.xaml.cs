using System.Numerics;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Media;

namespace Jarvis_GlassLab;

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
    private const float PillHeight = 40f;

    /// <summary>Shared, live-tunable material (see <see cref="GlassToggle.Material"/> for the
    /// convention). Rest numbers were picked so the rim bend reads on a frosted card without the
    /// label losing contrast; lift numbers are the toggle thumb's lens scaled down for a slab
    /// that is much wider than it is tall (the thumb's 16px bend on a 40px pill wraps the card
    /// edge into a loop).</summary>
    public static class Material
    {
        public static Vector3 ClearTint = Vector3.One;
        public static Vector3 AccentTint => Jarvis_Glass.JarvisPalette.ToVector3(Jarvis_Glass.JarvisPalette.Current.AccentFill);
        public static float AccentTintAmount = Jarvis_Glass.JarvisPalette.AccentTintAmount;

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
        public static float LiftTint = 0.05f;
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

    // Hover is a slower, softer spring than the press so it reads as a glow rather than a snap.
    private const float HoverStiffness = 260f;
    private const float HoverDamping = 22f;

    private float _lift, _liftVelocity;
    private float _hover, _hoverVelocity;
    private bool _pressed;
    private bool _pointerOver;
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

        Loaded += (_, _) => { PublishShape(); StartAnimating(); };
        Unloaded += (_, _) => { StopAnimating(); GlassShapeRegistry.Remove(this); GlassTextRegistry.Remove(this); };
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
        Spring(ref _lift, ref _liftVelocity, liftTarget, dt, GlassToggle.SpringStiffness, GlassToggle.SpringDamping);
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

    private static void Spring(ref float value, ref float velocity, float target, float dt, float stiffness, float damping)
    {
        var accel = (target - value) * stiffness - velocity * damping;
        velocity += accel * dt;
        value += velocity * dt;
    }

    private void PublishShape()
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

        var m = Math.Max(_lift, GlassToggle.Material.ForceLift); // rest -> lift blend
        var grow = 1f + (Material.LiftScale - 1f) * m;

        var center = new Vector2((float)(bounds.X + bounds.Width * 0.5), (float)(bounds.Y + bounds.Height * 0.5)) * scale;
        var half = new Vector2((float)bounds.Width * 0.5f, PillHeight * 0.5f) * scale * grow;
        var radius = half.Y; // a true pill: corner radius is half the height

        var tintColor = IsAccent ? Material.AccentTint : Material.ClearTint;
        var restTint = IsAccent ? Material.AccentTintAmount : Material.RestTint;
        var liftTint = IsAccent ? Material.AccentTintAmount * 0.8f : Material.LiftTint;
        var tint = restTint + (liftTint - restTint) * m + Material.HoverTintBoost * _hover * (1f - m);

        GlassShapeRegistry.Publish(this,
            GlassShape.Create(center, half, radius, radius * Material.BezelFraction, GlassBezelProfile.Lens,
                refractionScale: (Material.RestRefraction + (Material.LiftRefraction - Material.RestRefraction) * m) * scale,
                specularIntensity: Material.RestSpecular + (Material.LiftSpecular - Material.RestSpecular) * m,
                layer: 2,
                tintColor: tintColor,
                tintAmount: Math.Clamp(tint, 0f, 1f),
                chromatic: Material.LiftChromatic * m,
                shadowStrength: Material.RestShadow + (Material.LiftShadow - Material.RestShadow) * m,
                shadowRadius: (Material.RestShadowRadius + (Material.LiftShadowRadius - Material.RestShadowRadius) * m) * scale,
                shadowOffsetY: Material.ShadowOffsetY * scale,
                edgeRing: Material.RestEdgeRing + (Material.LiftEdgeRing - Material.RestEdgeRing) * m,
                secondLight: GlassToggle.Material.SecondLight));

        // Caption on the button's own layer: composited after the slab, so it sits on the glass
        // (not bent by it) and scales with the lift like part of the slab.
        GlassTextRegistry.Publish(this,
            new GlassText(Text ?? "", center, Material.LabelSize * scale * grow, GlassText.SemiBold, Material.LabelColor, Layer: 2));
    }
}
