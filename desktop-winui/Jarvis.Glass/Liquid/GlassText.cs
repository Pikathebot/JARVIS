using System.Numerics;

namespace Jarvis_Glass;

/// <summary>
/// A run of text a control wants drawn INSIDE the glass stack rather than in XAML above it: the
/// renderer rasterises it with DirectWrite into a content texture that is composited right after
/// <see cref="Layer"/>'s passes, so every layer above (a lifted pill sliding across a segmented
/// control, say) refracts it like anything else beneath the lens. Positions and sizes are in
/// physical pixels like <see cref="GlassShape"/>.
/// </summary>
public readonly record struct GlassText(
    string Text,
    Vector2 Center,
    float FontSize,
    int FontWeight,
    Vector4 Color,
    int Layer)
{
    public const int Regular = 400;
    public const int SemiBold = 600;
}
