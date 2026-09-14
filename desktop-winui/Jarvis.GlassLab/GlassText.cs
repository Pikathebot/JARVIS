using System.Numerics;

namespace Jarvis_GlassLab;

/// <summary>
/// A run of text a control wants drawn INSIDE the glass stack rather than in XAML above it: the
/// renderer rasterises it with DirectWrite into a content texture that is composited right after
/// <see cref="Layer"/>'s passes, so every layer above (a lifted pill sliding across a segmented
/// control, say) refracts it like anything else beneath the lens. Positions and sizes are in
/// physical pixels like <see cref="GlassShape"/>.
/// </summary>
internal readonly record struct GlassText(
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

/// <summary>Keyed slots per publisher, snapshot per frame -- the <see cref="GlassShapeRegistry"/>
/// contract for text. Empty publishes are fine (a control with nothing to say this tick).</summary>
internal static class GlassTextRegistry
{
    private static readonly object Gate = new();
    private static readonly Dictionary<object, GlassText[]> Slots = new();
    private static GlassText[] _snapshot = Array.Empty<GlassText>();
    private static bool _dirty;

    public static void Publish(object owner, params GlassText[] texts)
    {
        lock (Gate)
        {
            Slots[owner] = texts;
            _dirty = true;
        }
    }

    public static void Remove(object owner)
    {
        lock (Gate)
        {
            if (Slots.Remove(owner)) _dirty = true;
        }
    }

    public static GlassText[] Snapshot()
    {
        lock (Gate)
        {
            if (_dirty)
            {
                _snapshot = Slots.Values.SelectMany(t => t).ToArray();
                _dirty = false;
            }
            return _snapshot;
        }
    }
}
