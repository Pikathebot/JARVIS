using System.Numerics;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Windows.UI;

namespace Jarvis_Glass;

/// <summary>
/// A glass panel on the liquid-glass stack: publishes its own bounds to the window's
/// <see cref="GlassScene"/> as one frosted, tinted slab (layer 1 by default -- the card the
/// controls on layers 2+ sit on and refract), and otherwise is an ordinary Grid whose children
/// draw above the glass. Successor to GlassPanel for windows hosted by <see cref="GlassHost"/>:
/// the panel paints nothing itself, the renderer behind the window does.
/// </summary>
public sealed class GlassSlab : Grid
{
    public static readonly DependencyProperty SlabCornerRadiusProperty = DependencyProperty.Register(
        nameof(SlabCornerRadius), typeof(double), typeof(GlassSlab), new PropertyMetadata(16.0, OnMaterialChanged));

    public double SlabCornerRadius
    {
        get => (double)GetValue(SlabCornerRadiusProperty);
        set => SetValue(SlabCornerRadiusProperty, value);
    }

    public static readonly DependencyProperty MaterialProperty = DependencyProperty.Register(
        nameof(Material), typeof(GlassMaterial), typeof(GlassSlab), new PropertyMetadata(GlassMaterial.Regular, OnMaterialChanged));

    /// <summary>Which glass this is (design system: clear, regular, thick, accent). Sets the tint
    /// colour and the tint floor for the current <see cref="JarvisPalette.Appearance"/>; see
    /// <see cref="EffectiveTint"/>.</summary>
    public GlassMaterial Material
    {
        get => (GlassMaterial)GetValue(MaterialProperty);
        set => SetValue(MaterialProperty, value);
    }

    public static readonly DependencyProperty TintColorProperty = DependencyProperty.Register(
        nameof(TintColor), typeof(Color), typeof(GlassSlab), new PropertyMetadata(default(Color), OnMaterialChanged));

    /// <summary>Colour of the tint blended over the refracted backdrop (alpha ignored; see
    /// <see cref="TintAmount"/>). Unset (transparent), the palette's glass-tint for the current
    /// appearance, or accent-fill for <see cref="GlassMaterial.Accent"/>.</summary>
    public Color TintColor
    {
        get => (Color)GetValue(TintColorProperty);
        set => SetValue(TintColorProperty, value);
    }

    public static readonly DependencyProperty TintAmountProperty = DependencyProperty.Register(
        nameof(TintAmount), typeof(double), typeof(GlassSlab), new PropertyMetadata(0.2, OnMaterialChanged));

    /// <summary>0 = clear glass, 1 = opaque fill. The dark-appearance amount; light and high
    /// contrast raise it to their floor for the <see cref="Material"/> (see <see cref="EffectiveTint"/>).</summary>
    public double TintAmount
    {
        get => (double)GetValue(TintAmountProperty);
        set => SetValue(TintAmountProperty, value);
    }

    public static readonly DependencyProperty FrostProperty = DependencyProperty.Register(
        nameof(Frost), typeof(double), typeof(GlassSlab), new PropertyMetadata(10.0, OnMaterialChanged));

    /// <summary>Gaussian frost radius in DIPs (0 = clear).</summary>
    public double Frost
    {
        get => (double)GetValue(FrostProperty);
        set => SetValue(FrostProperty, value);
    }

    public static readonly DependencyProperty BezelWidthProperty = DependencyProperty.Register(
        nameof(BezelWidth), typeof(double), typeof(GlassSlab), new PropertyMetadata(10.0, OnMaterialChanged));

    public double BezelWidth
    {
        get => (double)GetValue(BezelWidthProperty);
        set => SetValue(BezelWidthProperty, value);
    }

    public static readonly DependencyProperty RefractionProperty = DependencyProperty.Register(
        nameof(Refraction), typeof(double), typeof(GlassSlab), new PropertyMetadata(6.0, OnMaterialChanged));

    /// <summary>Pixels of displacement at the bezel's steepest point (DIPs).</summary>
    public double Refraction
    {
        get => (double)GetValue(RefractionProperty);
        set => SetValue(RefractionProperty, value);
    }

    public static readonly DependencyProperty SpecularProperty = DependencyProperty.Register(
        nameof(Specular), typeof(double), typeof(GlassSlab), new PropertyMetadata(0.5, OnMaterialChanged));

    public double Specular
    {
        get => (double)GetValue(SpecularProperty);
        set => SetValue(SpecularProperty, value);
    }

    // Intentionally hides UIElement.Shadow (a composition ThemeShadow): the slab's shadow is
    // rendered by the glass pipeline, not the XAML compositor.
    public static new readonly DependencyProperty ShadowProperty = DependencyProperty.Register(
        nameof(Shadow), typeof(double), typeof(GlassSlab), new PropertyMetadata(0.32, OnMaterialChanged));

    /// <summary>Strength (0..1) of the soft shadow the slab drops onto the layer beneath it --
    /// the desktop for a panel, the panel for a nested sheet. What grounds a slab instead of
    /// leaving it as a bent rectangle floating in the capture.</summary>
    public new double Shadow
    {
        get => (double)GetValue(ShadowProperty);
        set => SetValue(ShadowProperty, value);
    }

    public static readonly DependencyProperty ShadowRadiusProperty = DependencyProperty.Register(
        nameof(ShadowRadius), typeof(double), typeof(GlassSlab), new PropertyMetadata(28.0, OnMaterialChanged));

    /// <summary>How far the shadow reaches out from the outline, in DIPs.</summary>
    public double ShadowRadius
    {
        get => (double)GetValue(ShadowRadiusProperty);
        set => SetValue(ShadowRadiusProperty, value);
    }

    public static readonly DependencyProperty ShadowOffsetYProperty = DependencyProperty.Register(
        nameof(ShadowOffsetY), typeof(double), typeof(GlassSlab), new PropertyMetadata(8.0, OnMaterialChanged));

    /// <summary>Downward offset of the shadow in DIPs, so it reads as light from above.</summary>
    public double ShadowOffsetY
    {
        get => (double)GetValue(ShadowOffsetYProperty);
        set => SetValue(ShadowOffsetYProperty, value);
    }

    public static readonly DependencyProperty LayerProperty = DependencyProperty.Register(
        nameof(Layer), typeof(int), typeof(GlassSlab), new PropertyMetadata(1, OnMaterialChanged));

    /// <summary>Layer the slab publishes on. Unset, it is 1 for a top-level panel and one above
    /// the nearest ancestor slab for a nested one (a message bubble inside the chat panel is
    /// layer 2), so nesting just works; set it explicitly to override.</summary>
    public int Layer
    {
        get => ReadLocalValue(LayerProperty) == DependencyProperty.UnsetValue ? AutoLayer() : (int)GetValue(LayerProperty);
        set => SetValue(LayerProperty, value);
    }

    private int AutoLayer()
    {
        DependencyObject? node = this;
        while (node is not null)
        {
            node = Microsoft.UI.Xaml.Media.VisualTreeHelper.GetParent(node);
            if (node is GlassSlab slab) return slab.Layer + 1;
        }
        return 1;
    }

    /// <summary>The layer a control inside <paramref name="element"/>'s slab should publish its
    /// lowest shape on: one above the nearest ancestor GlassSlab (so a control in a layer-2
    /// settings sheet refracts the sheet, not the panel beneath it), or 2 when there is none.</summary>
    /// <summary>True when the element or any ancestor is Collapsed. Collapsing a panel does not
    /// unload or re-layout its children, so their bounds stay stale and non-empty -- a control
    /// has to check this itself or its glass outlives the panel it was in.</summary>
    public static bool IsCollapsedInTree(UIElement element)
    {
        DependencyObject? node = element;
        while (node is not null)
        {
            if (node is UIElement ui && ui.Visibility == Visibility.Collapsed) return true;
            node = Microsoft.UI.Xaml.Media.VisualTreeHelper.GetParent(node);
        }
        return false;
    }

    public static int BaseLayerFor(UIElement element)
    {
        DependencyObject? node = element;
        while (node is not null)
        {
            node = Microsoft.UI.Xaml.Media.VisualTreeHelper.GetParent(node);
            if (node is GlassSlab slab) return slab.Layer + 1;
        }
        return 2;
    }

    /// <summary>The window-px rect a control inside <paramref name="element"/> is visually clipped
    /// to: the viewport of the nearest ancestor ScrollViewer, else the nearest GlassSlab's bounds,
    /// else none (w = 0). Passed to GlassShape/GlassText so the renderer clips where XAML does.</summary>
    public static Vector4 ClipFor(UIElement element, float scale)
    {
        DependencyObject? node = element;
        while (node is not null)
        {
            node = Microsoft.UI.Xaml.Media.VisualTreeHelper.GetParent(node);
            if (node is ScrollViewer or GlassSlab)
            {
                var fe = (FrameworkElement)node;
                try
                {
                    var b = fe.TransformToVisual(null).TransformBounds(new Windows.Foundation.Rect(0, 0, fe.ActualWidth, fe.ActualHeight));
                    return new Vector4((float)b.X, (float)b.Y, (float)b.Width, (float)b.Height) * scale;
                }
                catch
                {
                    return default;
                }
            }
        }
        return default;
    }

    private static void OnMaterialChanged(DependencyObject d, DependencyPropertyChangedEventArgs e) => ((GlassSlab)d).Publish();

    private GlassScene? _scene;

    public GlassSlab()
    {
        Loaded += (_, _) =>
        {
            JarvisPalette.Changed += OnAppearanceChanged;
            ApplyOutline();
            Publish();
            GlassScroll.Track(this, Publish);
        };
        LayoutUpdated += (_, _) => Publish();
        Unloaded += (_, _) =>
        {
            JarvisPalette.Changed -= OnAppearanceChanged;
            _scene?.Remove(this);
            _scene = null;
        };
    }

    private void OnAppearanceChanged()
    {
        ApplyOutline();
        Publish();
    }

    /// <summary>High contrast draws a 1px outline on every slab (tokens: outline). XAML paints
    /// above the glass, so the Grid's own border lands on the rim.</summary>
    private void ApplyOutline()
    {
        var colors = JarvisPalette.Current;
        if (colors.DrawsOutline)
        {
            BorderBrush = new Microsoft.UI.Xaml.Media.SolidColorBrush(colors.Outline);
            BorderThickness = new Thickness(1);
            CornerRadius = new CornerRadius(SlabCornerRadius);
        }
        else if (BorderBrush is not null)
        {
            BorderBrush = null;
            BorderThickness = new Thickness(0);
        }
    }

    /// <summary>The tint colour and amount actually published: <see cref="TintColor"/> (or the
    /// palette's glass-tint) at <see cref="TintAmount"/>, raised to the appearance's floor for
    /// regular and thick glass; accent glass tints toward accent-fill at glass-accent-tint.
    /// Dark's floors equal the tuned dark values, so dark renders as before.</summary>
    public (Color Color, float Amount) EffectiveTint()
    {
        var colors = JarvisPalette.Current;
        var amount = (float)Math.Clamp(TintAmount, 0, 1);
        return Material switch
        {
            GlassMaterial.Accent => (colors.AccentFill, Math.Max(amount, JarvisPalette.AccentTintAmount)),
            GlassMaterial.Thick => (TintOrPalette(colors), Math.Max(amount, JarvisPalette.Appearance == JarvisAppearance.Dark ? 0f : colors.ThickTintFloor)),
            GlassMaterial.Regular => (TintOrPalette(colors), Math.Max(amount, JarvisPalette.Appearance == JarvisAppearance.Dark ? 0f : colors.RegularTintFloor)),
            _ => (TintOrPalette(colors), amount),
        };
    }

    private Color TintOrPalette(JarvisColors colors) => TintColor.A == 0 ? colors.GlassTint : TintColor;

    private void Publish()
    {
        if (!IsLoaded || XamlRoot is null) return;
        if (_scene is null)
        {
            _scene = GlassScene.Find(this);
            if (_scene is null) return;
            ulong id = 0; try { id = XamlRoot.ContentIslandEnvironment.AppWindowId.Value; } catch { }
            GlassLog.Write($"slab '{Name}' bound to scene of window 0x{id:X} (scene 0x{_scene.GetHashCode():X}) size {ActualWidth}x{ActualHeight}");
        }
        if (IsCollapsedInTree(this) || ActualWidth <= 0 || ActualHeight <= 0)
        {
            // A collapsed panel (the right sheet) must take its slab with it.
            _scene.Remove(this);
            return;
        }

        var scale = (float)XamlRoot.RasterizationScale;
        Windows.Foundation.Rect b;
        try
        {
            b = TransformToVisual(null).TransformBounds(new Windows.Foundation.Rect(0, 0, ActualWidth, ActualHeight));
        }
        catch
        {
            return;
        }

        var center = new Vector2((float)(b.X + b.Width / 2), (float)(b.Y + b.Height / 2)) * scale;
        var half = new Vector2((float)b.Width / 2, (float)b.Height / 2) * scale;
        var (tint, tintAmount) = EffectiveTint();
        var shadow = JarvisPalette.Current.HasShadows ? (float)Math.Clamp(Shadow, 0, 1) : 0f;
        _scene.Publish(this, GlassShape.Create(
            center, half,
            cornerRadius: (float)SlabCornerRadius * scale,
            bezelWidth: (float)BezelWidth * scale,
            GlassBezelProfile.Squircle,
            refractionScale: (float)Refraction * scale,
            specularIntensity: (float)Specular,
            layer: Layer,
            clip: ClipFor(this, scale),
            tintColor: new Vector3(tint.R / 255f, tint.G / 255f, tint.B / 255f),
            tintAmount: tintAmount,
            blurRadius: (float)Frost * scale,
            shadowStrength: shadow,
            shadowRadius: (float)ShadowRadius * scale,
            shadowOffsetY: (float)ShadowOffsetY * scale,
            secondLight: 0.33f));
    }
}
