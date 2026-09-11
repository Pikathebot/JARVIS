using Microsoft.Graphics.Canvas;
using Microsoft.Graphics.Canvas.Brushes;
using Microsoft.Graphics.Canvas.Effects;
using Microsoft.Graphics.Canvas.Geometry;
using Microsoft.Graphics.Canvas.UI.Xaml;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Windows.Foundation;
using Windows.Graphics.DirectX;
using Windows.UI;

namespace Jarvis_Glass;

/// <summary>
/// Tier A shader glass: the material layer drawn above GlassPanel's Tier B acrylic.
///
/// It is drawn through Win2D's immediate-mode CanvasDrawingSession rather than the Composition
/// effect graph, because the effects that make a surface read as *glass* rather than as a tinted
/// rectangle — DisplacementMapEffect, TurbulenceEffect — are [NoComposition] and cannot enter a
/// CompositionEffectFactory graph at all.
///
/// **What supplies the backdrop.** The original design refracted a blurred crop of the desktop
/// wallpaper file. That premise fails outright under a live-wallpaper tool: Wallpaper Engine sets
/// the real wallpaper to a flat placeholder (a solid black JPEG) and animates a layer we cannot
/// read, so every panel sampling it rendered as a black slab — darker than the un-shaded panels
/// around it, which is what made the shell look like flat cards instead of glass. So the material
/// no longer *depends* on a backdrop sample: its body is a designed void gradient, and the
/// wallpaper is used only as an enrichment when <see cref="WallpaperBitmapCache.HasUsableWallpaper"/>
/// says one exists with real detail in it. The glass identity comes from the material qualities
/// layered over that body — edge bevel, pointer-tracked sheen, top light, film grain — which are
/// deterministic and look correct on any desktop.
///
/// Sampling what is *actually* on screen (live capture, which would pick up an animated wallpaper
/// and windows behind) is the planned follow-up; see PLAN.md. Wallpaper parallax-on-drag is
/// deferred with it, since it only matters once there is something worth parallaxing.
///
/// Wraps a CanvasControl (sealed, so composition rather than subclassing) — see LiquidGlassCanvas.xaml.
/// </summary>
public sealed partial class LiquidGlassCanvas : UserControl
{
    public static readonly DependencyProperty GlassCornerRadiusProperty = DependencyProperty.Register(
        nameof(GlassCornerRadius), typeof(double), typeof(LiquidGlassCanvas),
        new PropertyMetadata(16.0, (d, _) => ((LiquidGlassCanvas)d).Canvas.Invalidate()));

    public double GlassCornerRadius
    {
        get => (double)GetValue(GlassCornerRadiusProperty);
        set => SetValue(GlassCornerRadiusProperty, value);
    }

    public static readonly DependencyProperty BodyTopColorProperty = DependencyProperty.Register(
        nameof(BodyTopColor), typeof(Color), typeof(LiquidGlassCanvas),
        new PropertyMetadata(Color.FromArgb(150, 32, 43, 62), (d, _) => ((LiquidGlassCanvas)d).Canvas.Invalidate()));

    /// <summary>Top of the body gradient. Deliberately *lighter* than the shell around it — a
    /// glass slab catches more light than the void it floats over, and the previous near-black
    /// body was the single biggest reason panels read as holes rather than surfaces.</summary>
    public Color BodyTopColor
    {
        get => (Color)GetValue(BodyTopColorProperty);
        set => SetValue(BodyTopColorProperty, value);
    }

    public static readonly DependencyProperty BodyBottomColorProperty = DependencyProperty.Register(
        nameof(BodyBottomColor), typeof(Color), typeof(LiquidGlassCanvas),
        new PropertyMetadata(Color.FromArgb(170, 10, 15, 25), (d, _) => ((LiquidGlassCanvas)d).Canvas.Invalidate()));

    /// <summary>Bottom of the body gradient — deeper and more opaque, so the slab has weight.</summary>
    public Color BodyBottomColor
    {
        get => (Color)GetValue(BodyBottomColorProperty);
        set => SetValue(BodyBottomColorProperty, value);
    }

    public static readonly DependencyProperty AccentColorProperty = DependencyProperty.Register(
        nameof(AccentColor), typeof(Color), typeof(LiquidGlassCanvas),
        new PropertyMetadata(Color.FromArgb(255, 6, 182, 212), (d, _) => ((LiquidGlassCanvas)d).Canvas.Invalidate()));

    /// <summary>The JARVIS cyan, used in the sheen's mid stop and the rim's cool edge.</summary>
    public Color AccentColor
    {
        get => (Color)GetValue(AccentColorProperty);
        set => SetValue(AccentColorProperty, value);
    }

    public static readonly DependencyProperty TintColorProperty = DependencyProperty.Register(
        nameof(TintColor), typeof(Color), typeof(LiquidGlassCanvas),
        new PropertyMetadata(Color.FromArgb(0, 0, 0, 0), (d, _) => ((LiquidGlassCanvas)d).Canvas.Invalidate()));

    /// <summary>Optional extra wash above the whole material. Transparent by default: the body
    /// gradient already carries the palette, and stacking GlassPanel's tint on top of it is what
    /// double-dimmed Tier A panels into near-black.</summary>
    public Color TintColor
    {
        get => (Color)GetValue(TintColorProperty);
        set => SetValue(TintColorProperty, value);
    }

    public static readonly DependencyProperty LiveCaptureEnabledProperty = DependencyProperty.Register(
        nameof(LiveCaptureEnabled), typeof(bool), typeof(LiquidGlassCanvas),
        new PropertyMetadata(false, OnLiveCaptureEnabledChanged));

    /// <summary>Set externally by GlassPanel.ApplyTier(), which is in turn driven by
    /// GlassQualityService -- true only when the panel opted in (GlassPanel.LiveCaptureRequested),
    /// the live-capture Settings toggle is on, and GlassQuality is Full. Starts/stops the shared
    /// LiveCaptureService reference count; DrawBody prefers its current frame over the wallpaper
    /// crop whenever one is available.</summary>
    public bool LiveCaptureEnabled
    {
        get => (bool)GetValue(LiveCaptureEnabledProperty);
        set => SetValue(LiveCaptureEnabledProperty, value);
    }

    private static void OnLiveCaptureEnabledChanged(DependencyObject d, DependencyPropertyChangedEventArgs e)
    {
        if (d is not LiquidGlassCanvas canvas) return;
        if ((bool)e.NewValue)
        {
            LiveCaptureService.Start();
        }
        else
        {
            LiveCaptureService.Stop();
        }
        canvas.Canvas.Invalidate();
    }

    public static readonly DependencyProperty EdgeRefractionEnabledProperty = DependencyProperty.Register(
        nameof(EdgeRefractionEnabled), typeof(bool), typeof(LiquidGlassCanvas),
        new PropertyMetadata(false, (d, _) => ((LiquidGlassCanvas)d).Canvas.Invalidate()));

    /// <summary>Set externally by GlassPanel.ApplyTier() -- when true and a backdrop crop was drawn
    /// (live capture or a usable wallpaper), DrawBody bends it through a bezel displacement map
    /// (Checkpoint C) instead of drawing it flat.</summary>
    public bool EdgeRefractionEnabled
    {
        get => (bool)GetValue(EdgeRefractionEnabledProperty);
        set => SetValue(EdgeRefractionEnabledProperty, value);
    }

    private static readonly float[] RefractionLookup = BezelDisplacementMap.BuildRefractionLookup();

    public static readonly DependencyProperty SheenEnabledProperty = DependencyProperty.Register(
        nameof(SheenEnabled), typeof(bool), typeof(LiquidGlassCanvas),
        new PropertyMetadata(true, (d, _) => ((LiquidGlassCanvas)d).Canvas.Invalidate()));

    /// <summary>Whether the pointer-tracked specular highlight is drawn. Cleared by the
    /// GlassQuality "Reduced" tier, which keeps the static material but stops the per-frame work.</summary>
    public bool SheenEnabled
    {
        get => (bool)GetValue(SheenEnabledProperty);
        set => SetValue(SheenEnabledProperty, value);
    }

    private static readonly Point SheenRest = new(0.3, 0.12);

    private Point _sheenTarget = SheenRest;
    private Point _sheenCurrent = SheenRest;
    private bool _pointerInside;

    public LiquidGlassCanvas()
    {
        InitializeComponent();
        Canvas.Draw += OnDraw;

        PointerMoved += (_, e) =>
        {
            var pos = e.GetCurrentPoint(this).Position;
            if (ActualWidth > 0 && ActualHeight > 0)
            {
                _pointerInside = true;
                _sheenTarget = new Point(pos.X / ActualWidth, pos.Y / ActualHeight);
            }
        };
        PointerExited += (_, _) =>
        {
            _pointerInside = false;
            _sheenTarget = SheenRest;
        };

        // The wallpaper resolves asynchronously, always after the first paint. Without this the
        // panel keeps its bitmap-less first frame forever.
        Loaded += (_, _) => WallpaperBitmapCache.Loaded += OnWallpaperResolved;
        Unloaded += (_, _) =>
        {
            WallpaperBitmapCache.Loaded -= OnWallpaperResolved;
            LiveCaptureService.FrameUpdated -= OnLiveFrameUpdated;
            WindowPositionService.Changed -= OnWindowPositionChanged;
            // A panel can be torn down (e.g. window closing) without LiveCaptureEnabled ever going
            // back to false first -- release the reference count here too, or the shared capture
            // session leaks a ref forever and never actually stops.
            if (LiveCaptureEnabled) LiveCaptureService.Stop();
        };
        LiveCaptureService.FrameUpdated += OnLiveFrameUpdated;
        WindowPositionService.Changed += OnWindowPositionChanged;
    }

    private void OnWallpaperResolved() => DispatcherQueue.TryEnqueue(() => Canvas.Invalidate());

    private void OnLiveFrameUpdated()
    {
        // LiveCaptureService.FrameUpdated fires from the capture frame pool's free-threaded
        // callback, not the UI thread -- reading a DependencyProperty (LiveCaptureEnabled) here
        // before dispatching throws RPC_E_WRONG_THREAD. Defer the read into the dispatched call.
        DispatcherQueue.TryEnqueue(() =>
        {
            if (LiveCaptureEnabled) Canvas.Invalidate();
        });
    }

    private void OnWindowPositionChanged()
    {
        // Only the live-capture source is cropped by screen position -- the wallpaper-file crop
        // stays on the simpler window-relative calc, so a window move is a no-op for it.
        if (LiveCaptureEnabled) DispatcherQueue.TryEnqueue(() => Canvas.Invalidate());
    }

    /// <summary>Advances the sheen toward the pointer and redraws — the owning GlassPanel drives
    /// this on a shared per-panel timer (see GlassPanel.xaml.cs).</summary>
    public void Tick()
    {
        if (!SheenEnabled) return;

        // Simple critically-damped ease rather than a physics spring — this is a cosmetic
        // highlight, not something that needs Composition's spring animation machinery.
        var dx = _sheenTarget.X - _sheenCurrent.X;
        var dy = _sheenTarget.Y - _sheenCurrent.Y;
        if (Math.Abs(dx) < 0.001 && Math.Abs(dy) < 0.001) return;

        _sheenCurrent = new Point(_sheenCurrent.X + dx * 0.15, _sheenCurrent.Y + dy * 0.15);
        Canvas.Invalidate();
    }

    public void Invalidate() => Canvas.Invalidate();

    private void OnDraw(CanvasControl sender, CanvasDrawEventArgs args)
    {
        var ds = args.DrawingSession;
        var width = (float)sender.ActualWidth;
        var height = (float)sender.ActualHeight;
        if (width <= 0 || height <= 0) return;

        var radius = (float)GlassCornerRadius;

        // Everything but the rim is clipped to the rounded body. The rim is drawn after the layer
        // closes so its stroke keeps its outer half instead of being shaved off by the clip.
        using (var clipGeometry = CanvasGeometry.CreateRoundedRectangle(sender, 0, 0, width, height, radius, radius))
        using (ds.CreateLayer(1.0f, clipGeometry))
        {
            DrawBody(sender, ds, width, height);
            DrawTopLight(sender, ds, width, height);
            if (SheenEnabled) DrawSheen(ds, width, height);
            DrawGrain(sender, ds, width, height);

            if (TintColor.A > 0) ds.FillRectangle(0, 0, width, height, TintColor);
        }

        DrawRim(sender, ds, width, height, radius);
    }

    /// <summary>
    /// The slab itself: a designed void gradient, optionally enriched by a blurred crop of a
    /// wallpaper that actually has detail in it.
    /// </summary>
    private void DrawBody(CanvasControl sender, CanvasDrawingSession ds, float width, float height)
    {
        var drewWallpaper = false;

        // Live capture wins over the static wallpaper file when it's actually running and has a
        // frame -- it can refract an animated live-wallpaper layer no file on disk contains. Falls
        // back to the wallpaper crop (below) when capture is off, still warming up, or failed to
        // start.
        var isLiveCapture = LiveCaptureEnabled;
        // When this window covers the entire display, WindowCaptureExclusion has excluded it from
        // capture (see LiveCaptureService) but there is nothing else on that monitor left to
        // reveal -- the captured frame is genuinely solid black, not a bug, just nothing to show.
        // Feeding that through would paint every panel black instead of falling back to the
        // wallpaper crop, so treat "no usable frame" the same as "capture not running yet".
        if (isLiveCapture && XamlRoot is not null && WindowPositionService.TryGetWindowBounds(XamlRoot, out var fullscreenCheckBounds))
        {
            var primaryBounds = Microsoft.UI.Windowing.DisplayArea.Primary.OuterBounds;
            var coversDisplay = fullscreenCheckBounds.X <= primaryBounds.X
                && fullscreenCheckBounds.Y <= primaryBounds.Y
                && fullscreenCheckBounds.X + fullscreenCheckBounds.Width >= primaryBounds.X + primaryBounds.Width
                && fullscreenCheckBounds.Y + fullscreenCheckBounds.Height >= primaryBounds.Y + primaryBounds.Height;
            if (coversDisplay)
            {
                isLiveCapture = false;
            }
        }
        var bitmap = isLiveCapture ? LiveCaptureService.CurrentFrame : null;
        if (bitmap is null)
        {
            isLiveCapture = false;
            bitmap = WallpaperBitmapCache.GetAsync(sender) is { IsCompletedSuccessfully: true } bitmapTask
                ? bitmapTask.Result
                : null;
        }

        if (bitmap is not null && XamlRoot is not null)
        {
            var rootWidth = (float)XamlRoot.Size.Width;
            var rootHeight = (float)XamlRoot.Size.Height;
            float srcX, srcY, srcW, srcH;

            // The live-capture bitmap spans a whole display in raw pixels, the same coordinate
            // space WindowPositionService reports window bounds in -- so once the window's screen
            // position is known, crop by *actual* screen position (Checkpoint B parallax) instead
            // of only where the panel sits within its own window. Falls back to the window-relative
            // calc below (same one the wallpaper-file source always uses) until a position is known
            // -- e.g. before WindowPositionService.Register has run for this window.
            if (isLiveCapture && WindowPositionService.TryGetWindowBounds(XamlRoot, out var windowBoundsPx))
            {
                var scale = (float)XamlRoot.RasterizationScale;
                var panelOffsetDips = TransformToVisual(XamlRoot.Content).TransformPoint(new Point(0, 0));
                var displayBounds = Microsoft.UI.Windowing.DisplayArea.Primary.OuterBounds;

                srcX = windowBoundsPx.X + (float)panelOffsetDips.X * scale - displayBounds.X;
                srcY = windowBoundsPx.Y + (float)panelOffsetDips.Y * scale - displayBounds.Y;
                srcW = width * scale;
                srcH = height * scale;

                // This crop reads as exactly what's behind the panel, at its true screen position,
                // only because LiveCaptureService excludes our own windows from capture while
                // running (see WindowCaptureExclusion) -- otherwise Windows.Graphics.Capture would
                // see the panel's own opaque pixels here (there is no "display minus this window"
                // capture mode), which is what an earlier version of this code sidestepped with an
                // approximate margin-offset sample. That's no longer needed now that the window is
                // genuinely absent from the captured frame at this exact spot.
                srcX = Math.Clamp(srcX, 0, Math.Max(0, bitmap.SizeInPixels.Width - 1));
                srcY = Math.Clamp(srcY, 0, Math.Max(0, bitmap.SizeInPixels.Height - 1));
                srcW = Math.Max(1, Math.Min(bitmap.SizeInPixels.Width - srcX, srcW));
                srcH = Math.Max(1, Math.Min(bitmap.SizeInPixels.Height - srcY, srcH));
            }
            else if (rootWidth > 0 && rootHeight > 0)
            {
                var offset = TransformToVisual(XamlRoot.Content).TransformPoint(new Point(0, 0));
                srcX = (float)(offset.X / rootWidth) * bitmap.SizeInPixels.Width;
                srcY = (float)(offset.Y / rootHeight) * bitmap.SizeInPixels.Height;
                srcW = Math.Max(1, Math.Min(bitmap.SizeInPixels.Width - srcX, width / rootWidth * bitmap.SizeInPixels.Width));
                srcH = Math.Max(1, Math.Min(bitmap.SizeInPixels.Height - srcY, height / rootHeight * bitmap.SizeInPixels.Height));
            }
            else
            {
                srcX = srcY = 0;
                srcW = srcH = 1;
            }

            {
                // The blur runs in the *source* bitmap's pixel space, but the result is then scaled
                // into a panel-sized rect. A fixed BlurAmount therefore produces a different
                // apparent blur per panel size; scaling by the src->dest ratio keeps the blur
                // visually constant at roughly 18px on screen wherever it is drawn.
                var scale = srcW / Math.Max(1f, width);
                var blur = new GaussianBlurEffect
                {
                    Source = bitmap,
                    BlurAmount = Math.Clamp(18.0f * scale, 4.0f, 100.0f),
                    BorderMode = EffectBorderMode.Hard,
                };

                // Glass oversaturates what it transmits slightly; without this the blurred crop
                // reads as grey mud once the body gradient is composited over it.
                using var saturated = new SaturationEffect { Source = blur, Saturation = 1.4f };

                // Checkpoint C: bend the crop near the panel's rounded edge through a bezel
                // displacement map instead of drawing it flat. The map is generated in the panel's
                // own DIP-sized coordinate space (see BezelDisplacementMap.Get below), so the
                // source needs to be resampled into that same space first -- DisplacementMapEffect
                // reads Source and Displacement pixel-for-pixel against each other, it does not
                // reconcile two different coordinate frames the way ds.DrawImage's own dest/src
                // rects do.
                CanvasBitmap? displacementMap = null;
                float bezelWidthPx = 0f;
                if (EdgeRefractionEnabled)
                {
                    bezelWidthPx = Math.Max(8f, (float)GlassCornerRadius * 0.6f);
                    displacementMap = BezelDisplacementMap.Get(
                        sender, (int)MathF.Round(width), (int)MathF.Round(height),
                        (float)GlassCornerRadius, bezelWidthPx, RefractionLookup);
                }

                if (displacementMap is not null)
                {
                    using var resampled = new Transform2DEffect
                    {
                        Source = saturated,
                        // Maps the crop rect (in bitmap pixel space) onto the panel's own
                        // width x height (in DIPs) -- the same space the displacement map was
                        // rasterized in.
                        TransformMatrix = System.Numerics.Matrix3x2.CreateTranslation(-srcX, -srcY) *
                            System.Numerics.Matrix3x2.CreateScale(width / srcW, height / srcH),
                    };
                    using var displaced = new DisplacementMapEffect
                    {
                        Source = resampled,
                        Displacement = displacementMap,
                        Amount = Math.Max(8f, bezelWidthPx * 0.9f),
                        XChannelSelect = EffectChannelSelect.Red,
                        YChannelSelect = EffectChannelSelect.Green,
                    };

                    ds.DrawImage(displaced, new Rect(0, 0, width, height), new Rect(0, 0, width, height));
                }
                else
                {
                    ds.DrawImage(saturated,
                        new Rect(0, 0, width, height),
                        new Rect(srcX, srcY, srcW, srcH));
                }

                drewWallpaper = true;
            }
        }

        // Over a real wallpaper the body acts as a tint and must stay light enough to see through;
        // with no wallpaper it *is* the surface and carries full weight.
        var alphaScale = drewWallpaper ? 0.55f : 1.0f;

        using var bodyBrush = new CanvasLinearGradientBrush(
            sender, WithAlphaScale(BodyTopColor, alphaScale), WithAlphaScale(BodyBottomColor, alphaScale))
        {
            StartPoint = new System.Numerics.Vector2(0, 0),
            EndPoint = new System.Numerics.Vector2(width * 0.35f, height),
        };
        ds.FillRectangle(0, 0, width, height, bodyBrush);
    }

    /// <summary>Light falls from above: a bright band decaying down the top third, which is what
    /// makes a flat rectangle read as a surface with an orientation.</summary>
    private void DrawTopLight(CanvasControl sender, CanvasDrawingSession ds, float width, float height)
    {
        using var brush = new CanvasLinearGradientBrush(
            sender,
            new[]
            {
                new CanvasGradientStop { Position = 0.0f, Color = Color.FromArgb(28, 255, 255, 255) },
                new CanvasGradientStop { Position = 0.45f, Color = Color.FromArgb(8, 255, 255, 255) },
                new CanvasGradientStop { Position = 1.0f, Color = Color.FromArgb(0, 255, 255, 255) },
            })
        {
            StartPoint = new System.Numerics.Vector2(0, 0),
            EndPoint = new System.Numerics.Vector2(0, height * 0.55f),
        };
        ds.FillRectangle(0, 0, width, height, brush);
    }

    /// <summary>
    /// The pointer-tracked specular. Sized off the *smaller* dimension — the previous
    /// 0.6 * max(w, h) made the highlight larger than a tall sidebar, so it washed the whole panel
    /// evenly instead of reading as a moving highlight.
    /// </summary>
    private void DrawSheen(CanvasDrawingSession ds, float width, float height)
    {
        var cx = (float)(_sheenCurrent.X * width);
        var cy = (float)(_sheenCurrent.Y * height);
        var radius = Math.Max(80f, Math.Min(width, height) * 1.15f);

        // Brighter while the pointer is actually on the panel — the glass responds to being touched.
        var coreAlpha = (byte)(_pointerInside ? 52 : 30);
        var accent = AccentColor;

        using var brush = new CanvasRadialGradientBrush(
            ds,
            new[]
            {
                new CanvasGradientStop { Position = 0.0f, Color = Color.FromArgb(coreAlpha, 255, 255, 255) },
                new CanvasGradientStop { Position = 0.35f, Color = Color.FromArgb((byte)(coreAlpha / 2), 226, 240, 255) },
                new CanvasGradientStop { Position = 0.7f, Color = Color.FromArgb(16, accent.R, accent.G, accent.B) },
                new CanvasGradientStop { Position = 1.0f, Color = Color.FromArgb(0, accent.R, accent.G, accent.B) },
            })
        {
            Center = new System.Numerics.Vector2(cx, cy),
            RadiusX = radius,
            RadiusY = radius,
        };
        ds.FillRectangle(0, 0, width, height, brush);
    }

    /// <summary>
    /// A tiled noise wash. Acrylic has grain and a pure gradient does not; without this the
    /// material looks like plastic vector art rather than a physical surface. Cheap — one small
    /// bitmap generated once and wrapped over the panel.
    /// </summary>
    private void DrawGrain(CanvasControl sender, CanvasDrawingSession ds, float width, float height)
    {
        var noise = GrainBitmap.Get(sender);
        if (noise is null) return;

        using var brush = new CanvasImageBrush(sender, noise)
        {
            ExtendX = CanvasEdgeBehavior.Wrap,
            ExtendY = CanvasEdgeBehavior.Wrap,
            SourceRectangle = new Rect(0, 0, noise.SizeInPixels.Width, noise.SizeInPixels.Height),
            Opacity = 0.05f,
        };
        ds.FillRectangle(0, 0, width, height, brush);
    }

    /// <summary>
    /// The bevel. A uniform hairline border is what a *card* has; glass has a bright lit edge
    /// where light enters at the top-left and a faint one where it leaves, plus a cool inner
    /// bounce along the bottom.
    /// </summary>
    private void DrawRim(CanvasControl sender, CanvasDrawingSession ds, float width, float height, float radius)
    {
        const float stroke = 1.0f;
        var inset = stroke / 2f;
        var accent = AccentColor;

        using (var geometry = CanvasGeometry.CreateRoundedRectangle(
            sender, inset, inset, width - stroke, height - stroke, radius, radius))
        using (var brush = new CanvasLinearGradientBrush(
            sender,
            new[]
            {
                new CanvasGradientStop { Position = 0.0f, Color = Color.FromArgb(112, 255, 255, 255) },
                new CanvasGradientStop { Position = 0.35f, Color = Color.FromArgb(40, 255, 255, 255) },
                new CanvasGradientStop { Position = 1.0f, Color = Color.FromArgb(16, 255, 255, 255) },
            })
        {
            StartPoint = new System.Numerics.Vector2(0, 0),
            EndPoint = new System.Numerics.Vector2(width * 0.4f, height),
        })
        {
            ds.DrawGeometry(geometry, brush, stroke);
        }

        // Inner bounce: light that made it through the slab picking out the lower inside edge.
        using (var innerGeometry = CanvasGeometry.CreateRoundedRectangle(
            sender, 1.5f, 1.5f, width - 3f, height - 3f, Math.Max(1f, radius - 1.5f), Math.Max(1f, radius - 1.5f)))
        using (var innerBrush = new CanvasLinearGradientBrush(
            sender,
            new[]
            {
                new CanvasGradientStop { Position = 0.0f, Color = Color.FromArgb(0, accent.R, accent.G, accent.B) },
                new CanvasGradientStop { Position = 0.72f, Color = Color.FromArgb(0, accent.R, accent.G, accent.B) },
                new CanvasGradientStop { Position = 1.0f, Color = Color.FromArgb(46, accent.R, accent.G, accent.B) },
            })
        {
            StartPoint = new System.Numerics.Vector2(0, 0),
            EndPoint = new System.Numerics.Vector2(0, height),
        })
        {
            ds.DrawGeometry(innerGeometry, innerBrush, 1.0f);
        }
    }

    private static Color WithAlphaScale(Color color, float alphaScale) =>
        Color.FromArgb((byte)Math.Clamp(color.A * alphaScale, 0, 255), color.R, color.G, color.B);
}

/// <summary>
/// One small tiling noise bitmap shared by every panel's grain wash. Deterministic seed so the
/// grain does not shimmer between runs, and generated once because regenerating per draw would
/// cost more than the rest of the material combined.
/// </summary>
internal static class GrainBitmap
{
    private const int Size = 128;
    private static CanvasBitmap? _bitmap;

    public static CanvasBitmap? Get(ICanvasResourceCreator device)
    {
        if (_bitmap is not null) return _bitmap;

        try
        {
            var random = new Random(0x1A5E);
            var pixels = new byte[Size * Size * 4];
            for (var i = 0; i < Size * Size; i++)
            {
                // Monochrome grain in the alpha channel: the brush's own Opacity sets the strength,
                // so the per-pixel value only needs to carry the variation.
                var value = (byte)random.Next(96, 256);
                pixels[i * 4 + 0] = value;
                pixels[i * 4 + 1] = value;
                pixels[i * 4 + 2] = value;
                pixels[i * 4 + 3] = value;
            }

            _bitmap = CanvasBitmap.CreateFromBytes(
                device, pixels, Size, Size, DirectXPixelFormat.B8G8R8A8UIntNormalized);
            return _bitmap;
        }
        catch
        {
            return null;
        }
    }
}
