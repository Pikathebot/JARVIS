using System.Numerics;

namespace Jarvis_Glass;

/// <summary>
/// A run of text a control wants drawn INSIDE the glass stack rather than in XAML above it: the
/// renderer rasterises it with DirectWrite into a content texture that is composited right after
/// <see cref="Layer"/>'s passes, so every layer above (a lifted pill sliding across a segmented
/// control, say) refracts it like anything else beneath the lens. Positions and sizes are in
/// physical pixels like <see cref="GlassShape"/>. A positive <see cref="MaxWidth"/> keeps the
/// run inside that width, centred on <see cref="Center"/>, trimming the end to an ellipsis when
/// it doesn't fit (a user-named workspace can be any length; the pill it sits on can't).
/// </summary>
public readonly record struct GlassText(
    string Text,
    Vector2 Center,
    float FontSize,
    int FontWeight,
    Vector4 Color,
    int Layer,
    Vector4 Clip = default,
    float MaxWidth = 0f)
{
    public const int Regular = 400;
    public const int SemiBold = 600;
}
