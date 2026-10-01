using System.Numerics;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Media;

namespace Jarvis_Glass;

/// <summary>
/// A glass input: a clear pill with a soft lens rim (a gentler cousin of <see cref="GlassButton"/>)
/// carrying a real WinUI <see cref="TextBox"/>. Hover brightens the interior a touch; focus lifts
/// the slab -- slightly larger, the rim bends more, a deeper shadow and a slightly whiter tint
/// so the active field reads as the one that's listening. Same publish-every-tick contract as
/// the other controls: one shape on layer 2.
/// </summary>
public sealed partial class GlassTextField : UserControl
{

    public static class Material
    {
        public static Vector3 ClearTint = Vector3.One;
        public static Vector3 FocusTint = Vector3.One; // a slightly whiter glass, not a colour

        public static float BezelFraction = 0.45f;
        public static float RestRefraction = 5f;
        public static float RestSpecular = 0.55f;
        public static float RestEdgeRing = 0.15f;
        public static float FocusEdgeRing = 0.25f;
        public static float RestTint = 0.08f;
        public static float RestShadow = 0.14f;
        public static float RestShadowRadius = 6f;

        public static float HoverTintBoost = 0.05f;

        public static float FocusScale = 1.03f;
        public static float FocusRefraction = 9f;
        public static float FocusSpecular = 0.9f;
        public static float FocusTintAmount = 0.16f;
        public static float FocusChromatic = 0.02f;
        public static float FocusShadow = 0.26f;
        public static float FocusShadowRadius = 12f;

        public static float ShadowOffsetY = 2f;
    }

    private static readonly List<WeakReference<GlassTextField>> Instances = new();

    /// <summary>Re-publish every live field after a tuning change (called by GlassToggle.MaterialChanged).</summary>
    public static void RepublishAll()
    {
        foreach (var weak in Instances)
        {
            if (weak.TryGetTarget(out var field)) field.PublishShape();
        }
    }

    private const float HoverStiffness = 260f;
    private const float HoverDamping = 22f;

    private float _focus, _focusVelocity;
    private float _hover, _hoverVelocity;
    private bool _focused;
    private bool _pointerOver;
    private GlassScene? _scene;
    private bool _rendering;
    private long _lastTick;

    public static readonly DependencyProperty PlaceholderProperty = DependencyProperty.Register(
        nameof(Placeholder), typeof(string), typeof(GlassTextField), new PropertyMetadata("", (d, e) => ((GlassTextField)d).Input.PlaceholderText = (string)e.NewValue));

    public string Placeholder
    {
        get => (string)GetValue(PlaceholderProperty);
        set => SetValue(PlaceholderProperty, value);
    }

    public static readonly DependencyProperty AcceptsReturnProperty = DependencyProperty.Register(
        nameof(AcceptsReturn), typeof(bool), typeof(GlassTextField), new PropertyMetadata(false, (d, e) => ((GlassTextField)d).ApplyMultiline()));

    /// <summary>Multi-line composer mode: Enter inserts a newline, text wraps and the pill grows
    /// with the text up to <see cref="InputMaxHeight"/>.</summary>
    public bool AcceptsReturn
    {
        get => (bool)GetValue(AcceptsReturnProperty);
        set => SetValue(AcceptsReturnProperty, value);
    }

    public static readonly DependencyProperty InputMaxHeightProperty = DependencyProperty.Register(
        nameof(InputMaxHeight), typeof(double), typeof(GlassTextField), new PropertyMetadata(double.PositiveInfinity, (d, e) => ((GlassTextField)d).Input.MaxHeight = (double)e.NewValue));

    public double InputMaxHeight
    {
        get => (double)GetValue(InputMaxHeightProperty);
        set => SetValue(InputMaxHeightProperty, value);
    }

    public static readonly DependencyProperty InputPaddingProperty = DependencyProperty.Register(
        nameof(InputPadding), typeof(Thickness), typeof(GlassTextField), new PropertyMetadata(new Thickness(16, 8, 16, 0), (d, e) => ((GlassTextField)d).Input.Padding = (Thickness)e.NewValue));

    /// <summary>The text's inset inside the pill. A host that lays buttons over the pill's ends
    /// (the composer's attach, mic and send) widens the sides so text never runs under them.
    /// Top-only vertical padding: see the note on the TextBox in the XAML.</summary>
    public Thickness InputPadding
    {
        get => (Thickness)GetValue(InputPaddingProperty);
        set => SetValue(InputPaddingProperty, value);
    }

    private void ApplyMultiline()
    {
        Input.AcceptsReturn = AcceptsReturn;
        Input.TextWrapping = AcceptsReturn ? TextWrapping.Wrap : TextWrapping.NoWrap;
    }

    /// <summary>The inner TextBox's PreviewKeyDown (tunnelling), for Enter-to-send handling by
    /// the host: with AcceptsReturn on, the TextBox consumes Enter into a newline before a
    /// bubbling KeyDown handler ever sees it.</summary>
    public event KeyEventHandler? InputKeyDown;

    private void Input_KeyDown(object sender, KeyRoutedEventArgs e) => InputKeyDown?.Invoke(sender, e);

    /// <summary>Moves keyboard focus into the field.</summary>
    public new bool Focus(FocusState state) => Input.Focus(state);

    public string Text
    {
        get => Input.Text;
        set => Input.Text = value;
    }

    public event RoutedEventHandler? TextChanged;

    public GlassTextField()
    {
        InitializeComponent();
        Input.PlaceholderText = Placeholder;
        ApplyMultiline();
        Instances.Add(new WeakReference<GlassTextField>(this));

        Loaded += (_, _) => { PublishShape(); StartAnimating(); GlassScroll.Track(this, PublishShape); };
        Unloaded += (_, _) => { StopAnimating(); _scene?.Remove(this); _scene = null; };
        LayoutUpdated += (_, _) => PublishShape();

        PointerEntered += (_, _) => { _pointerOver = true; StartAnimating(); };
        PointerExited += (_, _) => { _pointerOver = false; StartAnimating(); };
    }

    private void Input_GotFocus(object sender, RoutedEventArgs e) { _focused = true; StartAnimating(); }
    private void Input_LostFocus(object sender, RoutedEventArgs e) { _focused = false; StartAnimating(); }
    private void Input_TextChanged(object sender, TextChangedEventArgs e) => TextChanged?.Invoke(this, new RoutedEventArgs());

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

        var focusTarget = _focused ? 1f : 0f;
        var hoverTarget = _pointerOver ? 1f : 0f;
        Spring(ref _focus, ref _focusVelocity, focusTarget, dt, GlassToggle.SpringStiffness, GlassToggle.SpringDamping);
        Spring(ref _hover, ref _hoverVelocity, hoverTarget, dt, HoverStiffness, HoverDamping);

        PublishShape();

        var settled = Math.Abs(_focus - focusTarget) < 0.0005f && Math.Abs(_focusVelocity) < 0.01f
                   && Math.Abs(_hover - hoverTarget) < 0.0005f && Math.Abs(_hoverVelocity) < 0.01f;
        if (settled)
        {
            _focus = focusTarget; _focusVelocity = 0f;
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

        var m = Math.Max(_focus, GlassToggle.Material.ForceLift);
        var grow = 1f + (Material.FocusScale - 1f) * m;

        var center = new Vector2((float)(bounds.X + bounds.Width * 0.5), (float)(bounds.Y + bounds.Height * 0.5)) * scale;
        var half = new Vector2((float)bounds.Width * 0.5f, (float)bounds.Height * 0.5f) * scale;
        // Lift grows the pill by the same few pixels on every side (scaled off its height), not
        // by a fraction of its width: a composer-wide field at 3% would swell 25px into the
        // attach and send buttons beside it.
        var liftPx = half.Y * (grow - 1f);
        half += new Vector2(liftPx, liftPx);
        var radius = half.Y;

        var tintColor = Vector3.Lerp(Material.ClearTint, Material.FocusTint, m);
        var tint = Material.RestTint + (Material.FocusTintAmount - Material.RestTint) * m + Material.HoverTintBoost * _hover * (1f - m);

        _scene.Publish(this,
            GlassShape.Create(center, half, radius, radius * Material.BezelFraction, GlassBezelProfile.Lens,
                refractionScale: (Material.RestRefraction + (Material.FocusRefraction - Material.RestRefraction) * m) * scale,
                specularIntensity: Material.RestSpecular + (Material.FocusSpecular - Material.RestSpecular) * m,
                layer: baseLayer,
                tintColor: tintColor,
                tintAmount: Math.Clamp(tint, 0f, 1f),
                chromatic: Material.FocusChromatic * m,
                shadowStrength: Material.RestShadow + (Material.FocusShadow - Material.RestShadow) * m,
                shadowRadius: (Material.RestShadowRadius + (Material.FocusShadowRadius - Material.RestShadowRadius) * m) * scale,
                shadowOffsetY: Material.ShadowOffsetY * scale,
                edgeRing: Material.RestEdgeRing + (Material.FocusEdgeRing - Material.RestEdgeRing) * m,
                clip: clip, secondLight: GlassToggle.Material.SecondLight));
    }
}
