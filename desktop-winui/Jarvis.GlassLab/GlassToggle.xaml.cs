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
    private const float ThumbRadius = 13f;
    private const float ThumbInset = 3f;

    // Material. Track: lip profile, bezel a bit over a third of the radius so the concave inner
    // lobe has room to read as concave. Thumb: a full lens (bezel = radius), squircle profile,
    // stronger displacement and highlight so it visibly magnifies whatever passes under it.
    private const float TrackBezel = 7f;
    private const float TrackRefraction = 10f;
    private const float ThumbRefraction = 18f;
    private const float TrackSpecular = 0.9f;
    private const float ThumbSpecular = 1.6f;
    private static readonly Vector3 OnTint = new(0.30f, 0.78f, 0.45f);
    private static readonly Vector3 OffTint = Vector3.One;
    private const float OnTintAmount = 0.35f;
    private const float OffTintAmount = 0.06f;
    private const float ThumbTintAmount = 0.12f;

    // Spring for the thumb's travel (0 = off, 1 = on) and its press scale.
    private const float Stiffness = 520f;
    private const float Damping = 24f;
    private const float PressScale = 1.12f;

    private float _travel;        // current position 0..1
    private float _travelVelocity;
    private float _scale = 1f;    // current thumb scale
    private float _scaleVelocity;
    private float _tintMix;       // 0 = off tint, 1 = on tint (springs with travel)
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
        var scaleTarget = _pressed ? PressScale : 1f;

        Spring(ref _travel, ref _travelVelocity, travelTarget, dt);
        Spring(ref _scale, ref _scaleVelocity, scaleTarget, dt);
        _tintMix = _travel;

        PublishShapes();

        var settled = Math.Abs(_travel - travelTarget) < 0.0005f && Math.Abs(_travelVelocity) < 0.01f
                   && Math.Abs(_scale - scaleTarget) < 0.0005f && Math.Abs(_scaleVelocity) < 0.01f;
        if (settled)
        {
            _travel = travelTarget; _travelVelocity = 0f;
            _scale = scaleTarget; _scaleVelocity = 0f;
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
        var thumbRadius = ThumbRadius * _scale * scale;
        var thumbCenter = new Vector2(thumbX, trackCenter.Y);

        var tint = Vector3.Lerp(OffTint, OnTint, _tintMix);
        var tintAmount = OffTintAmount + (OnTintAmount - OffTintAmount) * _tintMix;

        GlassShapeRegistry.Publish(this,
            GlassShape.Create(trackCenter, trackHalf, trackRadius, TrackBezel * scale, GlassBezelProfile.Lip,
                TrackRefraction * scale, TrackSpecular, layer: 1, tint, tintAmount),
            GlassShape.Create(thumbCenter, new Vector2(thumbRadius), thumbRadius, thumbRadius, GlassBezelProfile.Squircle,
                ThumbRefraction * scale, ThumbSpecular, layer: 2, Vector3.One, ThumbTintAmount));
    }
}
