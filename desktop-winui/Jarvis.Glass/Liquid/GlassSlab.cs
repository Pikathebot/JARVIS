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

    public static readonly DependencyProperty TintColorProperty = DependencyProperty.Register(
        nameof(TintColor), typeof(Color), typeof(GlassSlab), new PropertyMetadata(Color.FromArgb(255, 20, 23, 33), OnMaterialChanged));

    /// <summary>Colour of the tint blended over the refracted backdrop (alpha ignored; see
    /// <see cref="TintAmount"/>). The lab card's near-black blue-grey by default.</summary>
    public Color TintColor
    {
        get => (Color)GetValue(TintColorProperty);
        set => SetValue(TintColorProperty, value);
    }

    public static readonly DependencyProperty TintAmountProperty = DependencyProperty.Register(
        nameof(TintAmount), typeof(double), typeof(GlassSlab), new PropertyMetadata(0.45, OnMaterialChanged));

    /// <summary>0 = clear glass, 1 = opaque fill.</summary>
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

    public static readonly DependencyProperty LayerProperty = DependencyProperty.Register(
        nameof(Layer), typeof(int), typeof(GlassSlab), new PropertyMetadata(1, OnMaterialChanged));

    /// <summary>Glass layer index. Panels are 1; nest higher for a slab that should refract
    /// another slab beneath it.</summary>
    public int Layer
    {
        get => (int)GetValue(LayerProperty);
        set => SetValue(LayerProperty, value);
    }

    private static void OnMaterialChanged(DependencyObject d, DependencyPropertyChangedEventArgs e) => ((GlassSlab)d).Publish();

    private GlassScene? _scene;

    public GlassSlab()
    {
        Loaded += (_, _) => Publish();
        LayoutUpdated += (_, _) => Publish();
        Unloaded += (_, _) => { _scene?.Remove(this); _scene = null; };
    }

    private void Publish()
    {
        if (!IsLoaded || XamlRoot is null) return;
        _scene ??= GlassScene.Find(this);
        if (_scene is null) return;
        if (Visibility == Visibility.Collapsed || ActualWidth <= 0 || ActualHeight <= 0)
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
        var tint = TintColor;
        _scene.Publish(this, GlassShape.Create(
            center, half,
            cornerRadius: (float)SlabCornerRadius * scale,
            bezelWidth: (float)BezelWidth * scale,
            GlassBezelProfile.Squircle,
            refractionScale: (float)Refraction * scale,
            specularIntensity: (float)Specular,
            layer: Layer,
            tintColor: new Vector3(tint.R / 255f, tint.G / 255f, tint.B / 255f),
            tintAmount: (float)Math.Clamp(TintAmount, 0, 1),
            blurRadius: (float)Frost * scale,
            secondLight: 0.33f));
    }
}
