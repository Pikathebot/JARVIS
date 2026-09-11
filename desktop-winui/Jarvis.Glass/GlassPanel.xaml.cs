using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;

namespace Jarvis_Glass;

/// <summary>
/// The liquid-glass surface used throughout the shell (sidebar slab, chat surface, right-panel
/// sheet, HUD card, command palette). Two tiers behind one API: Tier B is SystemBackdropElement
/// (Acrylic) plus a tint and a gradient rim; Tier A adds a <see cref="LiquidGlassCanvas"/> that
/// draws the full material. Consumers only ever set <see cref="ShaderEnabled"/> and
/// <see cref="Quality"/> — see the remark in GlassPanel.xaml.
/// </summary>
public sealed partial class GlassPanel : UserControl
{
    public static readonly DependencyProperty GlassCornerRadiusProperty = DependencyProperty.Register(
        nameof(GlassCornerRadius), typeof(CornerRadius), typeof(GlassPanel),
        new PropertyMetadata(new CornerRadius(16)));

    public CornerRadius GlassCornerRadius
    {
        get => (CornerRadius)GetValue(GlassCornerRadiusProperty);
        set => SetValue(GlassCornerRadiusProperty, value);
    }

    public static readonly DependencyProperty TintBrushProperty = DependencyProperty.Register(
        nameof(TintBrush), typeof(Brush), typeof(GlassPanel),
        new PropertyMetadata(new SolidColorBrush(Windows.UI.Color.FromArgb(40, 5, 7, 11))));

    /// <summary>Overlay tint drawn above the acrylic — this is where the JARVIS void/cyan identity
    /// (kept per the plan's palette decision) is expressed on top of the neutral system material.
    /// Tier B only: it is deliberately NOT forwarded to the shader canvas, whose body gradient
    /// already carries the palette. Painting this near-black tint over the material as well is
    /// what turned shader panels into slabs darker than the un-shaded panels beside them, so
    /// ApplyTier hides the overlay outright while Tier A is on.</summary>
    public Brush TintBrush
    {
        get => (Brush)GetValue(TintBrushProperty);
        set => SetValue(TintBrushProperty, value);
    }

    public static readonly DependencyProperty ShaderEnabledProperty = DependencyProperty.Register(
        nameof(ShaderEnabled), typeof(bool), typeof(GlassPanel),
        new PropertyMetadata(false, OnShaderEnabledChanged));

    /// <summary>Switches this panel to Tier A (LiquidGlassCanvas) instead of Tier B alone.
    /// Off by default so existing call sites keep today's verified Tier B look until
    /// GlassQualityController (or a caller) opts a panel in.</summary>
    public bool ShaderEnabled
    {
        get => (bool)GetValue(ShaderEnabledProperty);
        set => SetValue(ShaderEnabledProperty, value);
    }

    private static void OnShaderEnabledChanged(DependencyObject d, DependencyPropertyChangedEventArgs e)
    {
        if (d is GlassPanel panel)
        {
            panel.ApplyTier();
        }
    }

    public static readonly DependencyProperty LiveCaptureRequestedProperty = DependencyProperty.Register(
        nameof(LiveCaptureRequested), typeof(bool), typeof(GlassPanel),
        new PropertyMetadata(false, OnLiveCaptureChanged));

    /// <summary>Per-panel opt-in to live-capture refraction, set by panel authors in XAML --
    /// separate from <see cref="ShaderEnabled"/> so existing designed-material Tier A panels are
    /// unaffected by default; live capture only actually runs once this AND
    /// <see cref="LiveCaptureAvailable"/> (pushed in externally, see GlassQualityService) are both
    /// true.</summary>
    public bool LiveCaptureRequested
    {
        get => (bool)GetValue(LiveCaptureRequestedProperty);
        set => SetValue(LiveCaptureRequestedProperty, value);
    }

    public static readonly DependencyProperty LiveCaptureAvailableProperty = DependencyProperty.Register(
        nameof(LiveCaptureAvailable), typeof(bool), typeof(GlassPanel),
        new PropertyMetadata(false, OnLiveCaptureChanged));

    /// <summary>Set externally by GlassQualityService: true only when the user's Settings toggle is
    /// on AND Quality == Full. GlassPanel itself never reads the setting or the quality tier
    /// directly -- it stays a passive DP surface, same as how Quality itself is pushed in today.</summary>
    public bool LiveCaptureAvailable
    {
        get => (bool)GetValue(LiveCaptureAvailableProperty);
        set => SetValue(LiveCaptureAvailableProperty, value);
    }

    private static void OnLiveCaptureChanged(DependencyObject d, DependencyPropertyChangedEventArgs e)
    {
        if (d is GlassPanel panel)
        {
            panel.ApplyTier();
        }
    }

    public static readonly DependencyProperty EdgeRefractionRequestedProperty = DependencyProperty.Register(
        nameof(EdgeRefractionRequested), typeof(bool), typeof(GlassPanel),
        new PropertyMetadata(false, OnEdgeRefractionChanged));

    /// <summary>Per-panel opt-in to the bezel edge-refraction displacement effect (Checkpoint C),
    /// same shape as <see cref="LiveCaptureRequested"/>. Set by panel authors in XAML.</summary>
    public bool EdgeRefractionRequested
    {
        get => (bool)GetValue(EdgeRefractionRequestedProperty);
        set => SetValue(EdgeRefractionRequestedProperty, value);
    }

    public static readonly DependencyProperty EdgeRefractionAvailableProperty = DependencyProperty.Register(
        nameof(EdgeRefractionAvailable), typeof(bool), typeof(GlassPanel),
        new PropertyMetadata(false, OnEdgeRefractionChanged));

    /// <summary>Set externally by GlassQualityService, mirroring <see cref="LiveCaptureAvailable"/> --
    /// reuses the same "Live-capture glass (experimental)" setting rather than a separate toggle,
    /// since refraction only has a real backdrop to bend once that's on (or a usable wallpaper).</summary>
    public bool EdgeRefractionAvailable
    {
        get => (bool)GetValue(EdgeRefractionAvailableProperty);
        set => SetValue(EdgeRefractionAvailableProperty, value);
    }

    private static void OnEdgeRefractionChanged(DependencyObject d, DependencyPropertyChangedEventArgs e)
    {
        if (d is GlassPanel panel)
        {
            panel.ApplyTier();
        }
    }

    public static readonly DependencyProperty GlassContentProperty = DependencyProperty.Register(
        nameof(GlassContent), typeof(object), typeof(GlassPanel), new PropertyMetadata(null));

    public object? GlassContent
    {
        get => GetValue(GlassContentProperty);
        set => SetValue(GlassContentProperty, value);
    }

    public static readonly DependencyProperty ContentPaddingProperty = DependencyProperty.Register(
        nameof(ContentPadding), typeof(Thickness), typeof(GlassPanel), new PropertyMetadata(new Thickness(0)));

    public Thickness ContentPadding
    {
        get => (Thickness)GetValue(ContentPaddingProperty);
        set => SetValue(ContentPaddingProperty, value);
    }

    public static readonly DependencyProperty QualityProperty = DependencyProperty.Register(
        nameof(Quality), typeof(GlassQuality), typeof(GlassPanel),
        new PropertyMetadata(GlassQuality.Full, OnQualityChanged));

    /// <summary>
    /// Rendering tier for this panel. <c>Full</c> is the animated material; <c>Reduced</c> keeps
    /// the material but stops the per-frame sheen (the only continuous work Tier A does);
    /// <c>System</c> drops to Tier B acrylic entirely. Driven app-wide by
    /// <see cref="GlassQualityController"/> so the glass yields the GPU to inference under load.
    /// </summary>
    public GlassQuality Quality
    {
        get => (GlassQuality)GetValue(QualityProperty);
        set => SetValue(QualityProperty, value);
    }

    private static void OnQualityChanged(DependencyObject d, DependencyPropertyChangedEventArgs e)
    {
        if (d is GlassPanel panel)
        {
            panel.ApplyTier();
        }
    }

    private readonly DispatcherTimer _sheenTimer = new() { Interval = TimeSpan.FromMilliseconds(16) };
    private bool _loaded;

    public GlassPanel()
    {
        InitializeComponent();
        _sheenTimer.Tick += (_, _) => ShaderCanvas.Tick();
        Loaded += (_, _) => { _loaded = true; ApplyTier(); };
        Unloaded += (_, _) => { _loaded = false; _sheenTimer.Stop(); };
    }

    /// <summary>
    /// Single place that decides what this panel actually renders, from ShaderEnabled + Quality.
    /// Keeping it in one method means the two properties can be set in either order, at any time,
    /// without the tiers getting out of sync.
    /// </summary>
    private void ApplyTier()
    {
        var shader = ShaderEnabled && Quality != GlassQuality.System;

        ShaderCanvas.Visibility = shader ? Visibility.Visible : Visibility.Collapsed;
        ShaderCanvas.SheenEnabled = Quality == GlassQuality.Full;
        // Capture is the expensive part of this material; gate it on the same Full-tier cutover
        // that governs the sheen's per-frame work, not a separate pressure threshold.
        ShaderCanvas.LiveCaptureEnabled = shader && LiveCaptureRequested && LiveCaptureAvailable && Quality == GlassQuality.Full;
        // Same Full-tier gate as live capture -- the displacement effect is real per-draw GPU cost.
        ShaderCanvas.EdgeRefractionEnabled = shader && EdgeRefractionRequested && EdgeRefractionAvailable && Quality == GlassQuality.Full;

        // Tier A owns the tint and the rim; leaving Tier B's versions on top would double-dim the
        // material and draw a second, flatter edge over the bevel.
        TintOverlay.Visibility = shader ? Visibility.Collapsed : Visibility.Visible;
        RimBorder.Visibility = shader ? Visibility.Collapsed : Visibility.Visible;

        if (shader && _loaded && Quality == GlassQuality.Full)
        {
            _sheenTimer.Start();
        }
        else
        {
            _sheenTimer.Stop();
            if (shader) ShaderCanvas.Invalidate();
        }
    }
}
