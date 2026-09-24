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
    private float _dragStartIndex;
    // How far the pointer has pulled beyond where the pill could follow (DIPs along the main
    // axis, signed), plus a little of the in-range motion. Springs back to zero on release. The
    // host uses it to let the card the menu sits in lean with the drag.
    private float _pull, _pullVelocity, _pullTarget;
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

    /// <summary>Raised every animation frame while the pill is being dragged or springing home
    /// with the current <see cref="DragPull"/>: the pointer's displacement beyond what the pill
    /// itself moved (DIPs along the main axis, signed), so a host can let the surrounding card
    /// lean in the drag direction and snap back.</summary>
    public event EventHandler<float>? DragPullChanged;

    public float DragPull => _pull;

    public static readonly DependencyProperty OrientationProperty = DependencyProperty.Register(
        nameof(Orientation), typeof(Orientation), typeof(GlassSegmented), new PropertyMetadata(Orientation.Horizontal, (d, _) => ((GlassSegmented)d).ApplyOrientation()));

    /// <summary>Vertical = a menu: rows of <see cref="RowHeight"/> stacked in the control, the
    /// selected one marked by the same lifting pill sliding up and down, labels left-aligned.
    /// No track is drawn -- the card the menu sits in is the track -- so the pill publishes on
    /// the control's base layer and the labels on the card's layer beneath it.</summary>
    public Orientation Orientation
    {
        get => (Orientation)GetValue(OrientationProperty);
        set => SetValue(OrientationProperty, value);
    }

    /// <summary>Row height in DIPs when vertical.</summary>
    public double RowHeight { get; set; } = 36;

    private bool IsVertical => Orientation == Orientation.Vertical;

    private void ApplyOrientation()
    {
        Height = IsVertical ? RowHeight * Count : TrackHeight;
        PublishShapes();
    }

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
        if (IsVertical) Height = RowHeight * Count;
        PublishShapes();
    }

    private void OnSelectedIndexChanged()
    {
        SelectionChanged?.Invoke(this, new RoutedEventArgs());
        StartAnimating();
    }

    /// <summary>Segment left edges and widths in DIPs. Segments are not equal: each takes a share
    /// of the track proportional to its label plus the pill's padding, so the pill at rest (which
    /// hugs its label) is what the user sees as "the selected segment" -- equal fifths of a
    /// 340px track left "Reasoning" clipped and "Files" swimming in space.</summary>
    private (float[] Left, float[] Width) SegmentLayout(float totalWidth)
    {
        var n = Count;
        var left = new float[n];
        var width = new float[n];
        if (IsVertical)
        {
            // Equal rows down the control: "left" is the row's top, "width" its height.
            for (var i = 0; i < n; i++) { width[i] = (float)RowHeight; left[i] = i * (float)RowHeight; }
            return (left, width);
        }
        if (_labelWidths.Length != n || totalWidth <= 0f)
        {
            for (var i = 0; i < n; i++) { width[i] = totalWidth / n; left[i] = i * width[i]; }
            return (left, width);
        }
        var sum = 0f;
        for (var i = 0; i < n; i++) sum += _labelWidths[i] + 2f * PillPadX;
        var k = sum > 0f ? totalWidth / sum : 0f;
        var x = 0f;
        for (var i = 0; i < n; i++)
        {
            width[i] = (_labelWidths[i] + 2f * PillPadX) * k;
            left[i] = x;
            x += width[i];
        }
        return (left, width);
    }

    /// <summary>Continuous segment index under a pointer x (DIPs): the segment centres are the
    /// integer positions, linear in between, so a drag carries the pill at the pointer's pace.</summary>
    private float IndexFromPointer(double x)
    {
        var (left, width) = SegmentLayout((float)(IsVertical ? ActualHeight : ActualWidth));
        var n = Count;
        if (n == 1 || width[0] <= 0f) return 0f;
        var px = (float)x;
        var c0 = left[0] + width[0] * 0.5f;
        if (px <= c0) return 0f;
        for (var i = 0; i < n - 1; i++)
        {
            var ca = left[i] + width[i] * 0.5f;
            var cb = left[i + 1] + width[i + 1] * 0.5f;
            if (px <= cb) return i + (px - ca) / Math.Max(1e-3f, cb - ca);
        }
        return n - 1;
    }

    private const float DragThresholdDip = 4f;
    /// <summary>How far the pointer must get from where it went down before a release counts as
    /// a drag rather than a tap. Separate from <see cref="DragThresholdDip"/>, which only decides
    /// when the thumb starts following: a click on a touchpad or a finger tap wobbles a few DIPs,
    /// and when that wobble was enough to turn the tap into a "drag" that went nowhere, the
    /// release settled back where it started and the tap was simply lost. Judged on the
    /// *furthest* the pointer got, not where it ended, so a quick out-and-back waggle is still
    /// a drag.</summary>
    private const float TapSlopDip = 6f;
    private float _maxDragDistance;

    private float MainAxis(Windows.Foundation.Point p) => (float)(IsVertical ? p.Y : p.X);

    /// <summary>Main-axis centre (DIPs) of a continuous index, for measuring how far the pill
    /// actually moved against how far the pointer did.</summary>
    private float CenterForIndex(float index)
    {
        var (left, width) = SegmentLayout((float)(IsVertical ? ActualHeight : ActualWidth));
        var n = Count;
        if (n == 0 || width.Length == 0) return 0f;
        var i = Math.Clamp((int)MathF.Floor(index), 0, n - 1);
        var j = Math.Min(i + 1, n - 1);
        var t = Math.Clamp(index - i, 0f, 1f);
        var ca = left[i] + width[i] * 0.5f;
        var cb = left[j] + width[j] * 0.5f;
        return ca + (cb - ca) * t;
    }

    private void OnPointerPressed(object sender, PointerRoutedEventArgs e)
    {
        _pressed = true;
        _dragging = false;
        _maxDragDistance = 0f;
        _dragStartX = MainAxis(e.GetCurrentPoint(this).Position);
        _dragTarget = SelectedIndex;
        _dragStartIndex = SelectedIndex;
        _pullTarget = 0f;
        CapturePointer(e.Pointer);
        StartAnimating();
        e.Handled = true;
    }

    private void OnPointerMoved(object sender, PointerRoutedEventArgs e)
    {
        if (!_pressed) return;
        var x = MainAxis(e.GetCurrentPoint(this).Position);
        _maxDragDistance = Math.Max(_maxDragDistance, Math.Abs(x - _dragStartX));
        if (!_dragging && Math.Abs(x - _dragStartX) < DragThresholdDip) return;
        _dragging = true;
        _dragTarget = IndexFromPointer(x);

        // Whatever the pointer moved that the pill did not (it stops at the last row) is pull
        // on the card, with a touch of the in-range motion so the card leans even mid-list.
        var pointerDelta = x - _dragStartX;
        var pillDelta = CenterForIndex(_dragTarget) - CenterForIndex(_dragStartIndex);
        _pullTarget = (pointerDelta - pillDelta) + pillDelta * 0.15f;
        e.Handled = true;
    }

    private void OnPointerReleased(object sender, PointerRoutedEventArgs e)
    {
        if (_pressed)
        {
            // Read the drag state BEFORE releasing capture: ReleasePointerCapture raises
            // PointerCaptureLost synchronously, and that handler clears _dragging -- so a
            // release that read it afterwards always saw false, took the tap branch, and
            // picked the row under the pointer instead of the one the puck was dragged
            // to, ignoring the drag entirely.
            var wasDragging = _dragging && _maxDragDistance >= TapSlopDip;
            _pressed = false;
            _dragging = false;
            ReleasePointerCapture(e.Pointer);
            var x = MainAxis(e.GetCurrentPoint(this).Position);
            var index = wasDragging ? (int)MathF.Round(_dragTarget) : (int)MathF.Round(IndexFromPointer(x));
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
        // Lift uses the press/release springs measured on the iOS switch and slider (both lift in
        // ~70-80 ms and settle back in ~200 ms with no bounce). The recording has no segmented
        // control, so its travel and stretch below are still unmeasured.
        if (liftTarget > _lift)
            Spring(ref _lift, ref _liftVelocity, liftTarget, dt, GlassToggle.LiftUpStiffness, GlassToggle.LiftUpDamping);
        else
            Spring(ref _lift, ref _liftVelocity, liftTarget, dt, GlassToggle.LiftDownStiffness, GlassToggle.LiftDownDamping);

        var pullTarget = _dragging ? _pullTarget : 0f;
        var pullBefore = _pull;
        Spring(ref _pull, ref _pullVelocity, pullTarget, dt, Stiffness, Damping);
        if (Math.Abs(_pull - pullBefore) > 0.01f || (Math.Abs(_pull) > 0.01f && !_dragging))
        {
            DragPullChanged?.Invoke(this, _pull);
        }

        PublishShapes();

        var settled = !_pressed
                   && Math.Abs(_travel - travelTarget) < 0.0005f && Math.Abs(_travelVelocity) < 0.01f
                   && Math.Abs(_lift - liftTarget) < 0.0005f && Math.Abs(_liftVelocity) < 0.01f
                   && Math.Abs(_pull) < 0.05f && Math.Abs(_pullVelocity) < 0.5f;
        if (settled)
        {
            _travel = travelTarget; _travelVelocity = 0f;
            _lift = liftTarget; _liftVelocity = 0f;
            if (_pull != 0f) { _pull = 0f; _pullVelocity = 0f; DragPullChanged?.Invoke(this, 0f); }
            PublishShapes();
            StopAnimating();
        }
    }

    /// <summary>Vertical (menu) publish: a pill the row's size sliding between rows, no track,
    /// labels left-aligned under it on the layer beneath.</summary>
    private void PublishVertical(GlassScene scene, Windows.Foundation.Rect bounds, float scale, Vector4 clip, int baseLayer)
    {
        var left = (float)bounds.X * scale;
        var top = (float)bounds.Y * scale;
        var width = (float)bounds.Width * scale;
        var rowH = (float)RowHeight * scale;
        var (rowTop, rowHeight) = SegmentLayout((float)bounds.Height);

        var m = Math.Max(_lift, GlassToggle.Material.ForceLift);
        var grow = 1f + (Material.LiftScale - 1f) * m;
        var lo = Math.Clamp((int)MathF.Floor(_travel), 0, Count - 1);
        var hi = Math.Clamp(lo + 1, 0, Count - 1);
        var f = Math.Clamp(_travel - lo, 0f, 1f);
        var rowCenterDip = (rowTop[lo] + rowHeight[lo] * 0.5f) + ((rowTop[hi] + rowHeight[hi] * 0.5f) - (rowTop[lo] + rowHeight[lo] * 0.5f)) * f;

        // The pill is the row minus the inset; lift grows it by a few px, not a fraction of
        // its width (a menu row is wide).
        var pillHalf = new Vector2(width * 0.5f - PillInset * scale, rowH * 0.5f - PillInset * scale);
        var liftPx = pillHalf.Y * (grow - 1f);
        pillHalf += new Vector2(liftPx, liftPx);
        var stretch = Math.Min(0.25f, Math.Abs(_travelVelocity) * PuckStretch) * m;
        pillHalf.Y *= 1f + stretch;
        var pillCenter = new Vector2(left + width * 0.5f, top + rowCenterDip * scale);
        var pillRadius = Math.Min(pillHalf.Y, 10f * scale);

        scene.Publish(this,
            GlassShape.Create(pillCenter, pillHalf, pillRadius, pillRadius * Material.LiftBezelFraction, GlassBezelProfile.Lens,
                refractionScale: Material.LiftRefraction * m * scale,
                specularIntensity: T.RestSpecular + (T.LiftSpecular - T.RestSpecular) * m,
                layer: baseLayer,
                tintColor: B.AccentTint,
                tintAmount: Material.PillRestTintAmount + (Material.PillLiftTintAmount - Material.PillRestTintAmount) * m,
                chromatic: T.LiftChromatic * m,
                shadowStrength: T.RestShadow + (T.LiftShadow - T.RestShadow) * m,
                shadowRadius: (T.RestShadowRadius + (T.LiftShadowRadius - T.RestShadowRadius) * m) * scale,
                shadowOffsetY: T.ShadowOffsetY * scale,
                edgeRing: T.LiftEdgeRing * m, clip: clip, secondLight: T.SecondLight));

        // Labels left-aligned (the renderer centres text, so centre each run on its own measured
        // width) on the card's layer beneath the pill; a dark copy rides the pill at rest.
        // A label longer than the row (menu items are user-named) is trimmed to an ellipsis
        // inside the pill's padding on both sides.
        var labelMax = Math.Max(1f, (float)bounds.Width - 2f * (PillInset + PillPadX));
        var texts = new List<GlassText>(_labels.Length * 2);
        for (var i = 0; i < _labels.Length; i++)
        {
            var near = Math.Clamp(1f - Math.Abs(_travel - i), 0f, 1f);
            var v = Material.LabelRestValue + (Material.LabelSelectedValue - Material.LabelRestValue) * near;
            var labelW = Math.Min(_labelWidths.Length == Count ? _labelWidths[i] : 0f, labelMax);
            var at = new Vector2(left + (PillInset + PillPadX + labelW * 0.5f) * scale, top + (rowTop[i] + rowHeight[i] * 0.5f) * scale);
            var size = Material.LabelSize * scale;
            var maxPx = labelMax * scale;
            texts.Add(new GlassText(_labels[i], at, size, GlassText.SemiBold, new Vector4(v, v, v, 1f), Layer: baseLayer - 1, Clip: clip, MaxWidth: maxPx));
            var onPill = near * (1f - m);
            if (onPill > 0.002f)
            {
                var d = Material.LabelSelectedValue;
                texts.Add(new GlassText(_labels[i], at, size, GlassText.SemiBold, new Vector4(d, d, d, onPill), Layer: baseLayer, Clip: clip, MaxWidth: maxPx));
            }
        }
        scene.PublishText(this, texts.ToArray());
    }

    /// <summary>Unmeasured (no segmented control in the reference recording): kept at its value
    /// before the toggle's own stretch was measured (2 x the toggle's old 0.012).</summary>
    private const float PuckStretch = 0.024f;

    /// <summary>Semi-implicit Euler in steps of at most 1/240 s, so a dropped frame (dt up to
    /// 50 ms) cannot make the stiffer press spring ring.</summary>
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

        if (IsVertical)
        {
            PublishVertical(_scene, bounds, scale, clip, baseLayer);
            return;
        }

        var left = (float)bounds.X * scale;
        var width = (float)bounds.Width * scale;
        var centerY = (float)(bounds.Y + bounds.Height * 0.5) * scale;
        var trackHalf = new Vector2(width * 0.5f, TrackHeight * 0.5f * scale);
        var trackCenter = new Vector2(left + width * 0.5f, centerY);
        var trackRadius = trackHalf.Y;

        var (segLeft, segWidth) = SegmentLayout((float)bounds.Width);
        var m = Math.Max(_lift, GlassToggle.Material.ForceLift);
        var grow = 1f + (Material.LiftScale - 1f) * m;
        // Pill width and position follow the label under it: between segments they ease from one
        // segment's to the next's along with the travel.
        var lo = Math.Clamp((int)MathF.Floor(_travel), 0, Count - 1);
        var hi = Math.Clamp(lo + 1, 0, Count - 1);
        var f = Math.Clamp(_travel - lo, 0f, 1f);
        var labelW = _labelWidths.Length == Count ? _labelWidths[lo] + (_labelWidths[hi] - _labelWidths[lo]) * f : segWidth[lo];
        var segWDip = segWidth[lo] + (segWidth[hi] - segWidth[lo]) * f;
        var segCenterDip = (segLeft[lo] + segWidth[lo] * 0.5f) + ((segLeft[hi] + segWidth[hi] * 0.5f) - (segLeft[lo] + segWidth[lo] * 0.5f)) * f;
        var pillW = Math.Min(segWDip - 2f * PillInset, labelW + 2f * PillPadX) * scale;
        var pillHalf = new Vector2(pillW * 0.5f, (TrackHeight * 0.5f - PillInset) * scale) * grow;
        // Stretch along the travel with velocity, like the toggle thumb.
        var stretch = Math.Min(0.25f, Math.Abs(_travelVelocity) * PuckStretch) * m;
        pillHalf.X *= 1f + stretch;
        var pillCenter = new Vector2(left + segCenterDip * scale, centerY);
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
            var at = new Vector2(left + (segLeft[i] + segWidth[i] * 0.5f) * scale, centerY);
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
