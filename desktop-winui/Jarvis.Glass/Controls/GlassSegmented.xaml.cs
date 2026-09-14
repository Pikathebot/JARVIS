using System.Numerics;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Media;
using B = Jarvis_Glass.GlassButton.Material;
using T = Jarvis_Glass.GlassToggle.Material;

namespace Jarvis_Glass;

/// <summary>
/// The iOS 26 segmented control: an opaque light-grey capsule track (layer 2) with the selected
/// segment marked by a white pill (layer 3) that is the toggle's thumb material stretched to a
/// segment -- opaque with a soft shadow at rest, lifting into a clear glass lens while pressed or
/// travelling, so it refracts the labels and track it slides across. The labels are drawn by the
/// renderer (<see cref="GlassText"/> on the track's layer, beneath the pill) rather than by XAML,
/// which is what lets the lens bend them. Tap a segment to select it; press and drag to carry the
/// pill along, release snaps to the nearest segment.
/// </summary>
public sealed partial class GlassSegmented : UserControl
{
    private const float TrackHeight = 36f;
    private const float PillInset = 3f;
    /// <summary>Horizontal padding of the pill around its label, in DIPs.</summary>
    private const float PillPadX = 14f;

    public static class Material
    {
        // Jarvis's dark shell: the track is the clear-glass button slab and the selected pill the
        // accent button's fill (both from GlassButton.Material), labels white -- not iOS's opaque
        // light-grey capsule the lab reproduced.
        public static float PillRestTintAmount => B.AccentTintAmount;
        public static float PillLiftTintAmount => B.AccentTintAmount * 0.8f;
        /// <summary>The selected pill grows by this factor when lifted.</summary>
        public static float LiftScale = 1.12f;
        public static float LabelSize = 13f;
        /// <summary>Label grey on the track and near-black under the pill.</summary>
        public static float LabelRestValue = 0.72f;
        public static float LabelSelectedValue = 1.0f;
        public static float LiftRefraction = 10f;
        public static float LiftBezelFraction = 0.5f;
    }

    private static readonly List<WeakReference<GlassSegmented>> Instances = new();

    /// <summary>Re-publish every live control after a tuning change (called by GlassToggle.MaterialChanged).</summary>
    public static void RepublishAll()
    {
        foreach (var weak in Instances)
        {
            if (weak.TryGetTarget(out var seg)) seg.PublishShapes();
        }
    }

    private const float Stiffness = 600f;
    private const float Damping = 30f;

    private string[] _labels = Array.Empty<string>();
    private float[] _labelWidths = Array.Empty<float>(); // DIPs, measured with a XAML TextBlock
    private float _travel;        // continuous selected index
    private float _travelVelocity;
    private float _lift, _liftVelocity;
    private bool _pressed;
    private bool _dragging;
    private float _dragStartX;
    private float _dragTarget;
    private GlassScene? _scene;
    private bool _rendering;
    private long _lastTick;

    public static readonly DependencyProperty ItemsProperty = DependencyProperty.Register(
        nameof(Items), typeof(string), typeof(GlassSegmented), new PropertyMetadata("One|Two|Three", (d, _) => ((GlassSegmented)d).RebuildLabels()));

    /// <summary>Pipe-separated segment labels (a string so it can be set from XAML).</summary>
    public string Items
    {
        get => (string)GetValue(ItemsProperty);
        set => SetValue(ItemsProperty, value);
    }

    public static readonly DependencyProperty SelectedIndexProperty = DependencyProperty.Register(
        nameof(SelectedIndex), typeof(int), typeof(GlassSegmented), new PropertyMetadata(0, (d, _) => ((GlassSegmented)d).OnSelectedIndexChanged()));

    public int SelectedIndex
    {
        get => (int)GetValue(SelectedIndexProperty);
        set => SetValue(SelectedIndexProperty, value);
    }

    public event RoutedEventHandler? SelectionChanged;

    private int Count => Math.Max(1, _labels.Length);

    public GlassSegmented()
    {
        InitializeComponent();
        RebuildLabels();
        _travel = SelectedIndex;
        Instances.Add(new WeakReference<GlassSegmented>(this));

        Loaded += (_, _) => { PublishShapes(); StartAnimating(); GlassScroll.Track(this, PublishShapes); };
        Unloaded += (_, _) => { StopAnimating(); _scene?.Remove(this); _scene = null; };
        LayoutUpdated += (_, _) => PublishShapes();

        PointerPressed += OnPointerPressed;
        PointerMoved += OnPointerMoved;
        PointerReleased += OnPointerReleased;
        PointerCanceled += (_, _) => { _pressed = false; _dragging = false; StartAnimating(); };
        PointerCaptureLost += (_, _) => { _pressed = false; _dragging = false; StartAnimating(); };
    }

    private void RebuildLabels()
    {
        _labels = (Items ?? "").Split('|', StringSplitOptions.RemoveEmptyEntries).Select(s => s.Trim()).ToArray();
        // The pill fits its label rather than the whole segment, so measure each one. A XAML
        // TextBlock in the same face/size/weight is close enough to the DirectWrite run the
        // renderer draws (same font, same DIP size).
        _labelWidths = _labels.Select(text =>
        {
            var probe = new TextBlock
            {
                Text = text,
                FontSize = Material.LabelSize,
                FontWeight = Microsoft.UI.Text.FontWeights.SemiBold,
                FontFamily = new FontFamily(GlassContentSurface.FontFamily),
            };
            probe.Measure(new Windows.Foundation.Size(double.PositiveInfinity, double.PositiveInfinity));
            return (float)probe.DesiredSize.Width;
        }).ToArray();
        PublishShapes();
    }

    private void OnSelectedIndexChanged()
    {
        SelectionChanged?.Invoke(this, new RoutedEventArgs());
        StartAnimating();
    }

    private float IndexFromPointer(double x)
    {
        var w = (float)ActualWidth / Count;
        return w <= 0 ? 0f : Math.Clamp((float)x / w - 0.5f, 0f, Count - 1);
    }

    private const float DragThresholdDip = 4f;

    private void OnPointerPressed(object sender, PointerRoutedEventArgs e)
    {
        _pressed = true;
        _dragging = false;
        _dragStartX = (float)e.GetCurrentPoint(this).Position.X;
        _dragTarget = SelectedIndex;
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
        _dragTarget = IndexFromPointer(x);
        e.Handled = true;
    }

    private void OnPointerReleased(object sender, PointerRoutedEventArgs e)
    {
        if (_pressed)
        {
            _pressed = false;
            ReleasePointerCapture(e.Pointer);
            var x = e.GetCurrentPoint(this).Position.X;
            var index = _dragging ? (int)MathF.Round(_dragTarget) : (int)MathF.Round(IndexFromPointer(x));
            _dragging = false;
            if (index == SelectedIndex) StartAnimating();
            SelectedIndex = Math.Clamp(index, 0, Count - 1);
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

        var travelTarget = _dragging ? _dragTarget : SelectedIndex;
        var travelling = Math.Abs(_travel - travelTarget) > 0.03f || Math.Abs(_travelVelocity) > 0.5f;
        var liftTarget = _pressed || travelling ? 1f : 0f;

        Spring(ref _travel, ref _travelVelocity, travelTarget, dt, Stiffness, Damping);
        Spring(ref _lift, ref _liftVelocity, liftTarget, dt, GlassToggle.SpringStiffness, GlassToggle.SpringDamping);

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

    private static void Spring(ref float value, ref float velocity, float target, float dt, float stiffness, float damping)
    {
        var accel = (target - value) * stiffness - velocity * damping;
        velocity += accel * dt;
        value += velocity * dt;
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
        if (bounds.Width <= 0 || bounds.Height <= 0 || Visibility == Visibility.Collapsed) { _scene.Remove(this); return; } // collapsed (or in a collapsed parent): take the glass with it
        var clip = GlassSlab.ClipFor(this, scale);

        var left = (float)bounds.X * scale;
        var width = (float)bounds.Width * scale;
        var centerY = (float)(bounds.Y + bounds.Height * 0.5) * scale;
        var trackHalf = new Vector2(width * 0.5f, TrackHeight * 0.5f * scale);
        var trackCenter = new Vector2(left + width * 0.5f, centerY);
        var trackRadius = trackHalf.Y;

        var segW = width / Count;
        var m = Math.Max(_lift, GlassToggle.Material.ForceLift);
        var grow = 1f + (Material.LiftScale - 1f) * m;
        // Pill width follows the label under it: between segments it eases from one label's
        // width to the next's along with the travel.
        var lo = Math.Clamp((int)MathF.Floor(_travel), 0, Count - 1);
        var hi = Math.Clamp(lo + 1, 0, Count - 1);
        var f = Math.Clamp(_travel - lo, 0f, 1f);
        var labelW = _labelWidths.Length == Count ? _labelWidths[lo] + (_labelWidths[hi] - _labelWidths[lo]) * f : segW / scale;
        var pillW = Math.Min(segW - 2f * PillInset * scale, (labelW + 2f * PillPadX) * scale);
        var pillHalf = new Vector2(pillW * 0.5f, (TrackHeight * 0.5f - PillInset) * scale) * grow;
        // Stretch along the travel with velocity, like the toggle thumb.
        var stretch = Math.Min(0.25f, Math.Abs(_travelVelocity) * GlassToggle.Material.ThumbStretch * 2f) * m;
        pillHalf.X *= 1f + stretch;
        var pillCenter = new Vector2(left + segW * (_travel + 0.5f), centerY);
        var pillRadius = pillHalf.Y;

        _scene.Publish(this,
            GlassShape.Create(trackCenter, trackHalf, trackRadius, trackRadius * B.BezelFraction, GlassBezelProfile.Lens,
                B.RestRefraction * scale, B.RestSpecular, layer: baseLayer, B.ClearTint, B.RestTint,
                shadowStrength: B.RestShadow, shadowRadius: B.RestShadowRadius * scale, shadowOffsetY: B.ShadowOffsetY * scale,
                edgeRing: B.RestEdgeRing, clip: clip, secondLight: T.SecondLight),
            GlassShape.Create(pillCenter, pillHalf, pillRadius, pillRadius * Material.LiftBezelFraction, GlassBezelProfile.Lens,
                refractionScale: Material.LiftRefraction * m * scale,
                specularIntensity: T.RestSpecular + (T.LiftSpecular - T.RestSpecular) * m,
                layer: baseLayer + 1,
                tintColor: B.AccentTint,
                tintAmount: Material.PillRestTintAmount + (Material.PillLiftTintAmount - Material.PillRestTintAmount) * m,
                chromatic: T.LiftChromatic * m,
                shadowStrength: T.RestShadow + (T.LiftShadow - T.RestShadow) * m,
                shadowRadius: (T.RestShadowRadius + (T.LiftShadowRadius - T.RestShadowRadius) * m) * scale,
                shadowOffsetY: T.ShadowOffsetY * scale,
                edgeRing: T.LiftEdgeRing * m, clip: clip, secondLight: T.SecondLight));

        // Labels on the track's layer so the pill (one layer up) refracts them. The selected
        // label reads dark on the white pill, the rest a muted grey on the track, blended by how
        // close the pill currently is to each segment so the swap happens as the pill arrives
        // rather than snapping ahead of it. At rest the pill is opaque, so the label beneath it
        // would vanish: a dark copy is drawn on the pill's own layer (on top of it) and fades out
        // as the pill lifts into clear glass, handing over to the refracted copy underneath.
        var texts = new List<GlassText>(_labels.Length * 2);
        for (var i = 0; i < _labels.Length; i++)
        {
            var near = Math.Clamp(1f - Math.Abs(_travel - i), 0f, 1f);
            var v = Material.LabelRestValue + (Material.LabelSelectedValue - Material.LabelRestValue) * near;
            var at = new Vector2(left + segW * (i + 0.5f), centerY);
            var size = Material.LabelSize * scale;
            texts.Add(new GlassText(_labels[i], at, size, GlassText.SemiBold, new Vector4(v, v, v, 1f), Layer: baseLayer, Clip: clip));
            var onPill = near * (1f - m);
            if (onPill > 0.002f)
            {
                var d = Material.LabelSelectedValue;
                texts.Add(new GlassText(_labels[i], at, size, GlassText.SemiBold, new Vector4(d, d, d, onPill), Layer: baseLayer + 1, Clip: clip));
            }
        }
        _scene.PublishText(this, texts.ToArray());
    }
}
