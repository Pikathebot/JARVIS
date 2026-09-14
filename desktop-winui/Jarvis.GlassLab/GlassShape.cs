using System.Numerics;
using System.Runtime.InteropServices;

namespace Jarvis_GlassLab;

/// <summary>
/// One glass slab for the renderer: a rounded rect in window pixel space with its own bezel
/// material. The panel is shape 0 on layer 0; every control (a toggle's track, its thumb, a
/// slider's rail...) is one or more shapes on higher layers. Layers render back-to-front and each
/// layer refracts the *output* of the layer below it, which is what makes a thumb bend the track
/// it sits on rather than the raw desktop -- the layering is the whole point of the kube.io
/// material, not a detail.
///
/// Layout mirrors GlassShape in the HLSL cbuffer exactly (six float4s, 96 bytes) -- a mismatch
/// here is a silent wrong-output bug, so the struct is explicit-layout and its size asserted.
/// </summary>
[StructLayout(LayoutKind.Sequential, Size = Size)]
internal struct GlassShape
{
    public const int Size = 96;

    /// <summary>xy = center (window px), zw = half size (window px).</summary>
    public Vector4 CenterHalfSize;

    /// <summary>x = corner radius, y = bezel width, z = profile (0 squircle / 1 lip),
    /// w = refraction scale (pixels of displacement at the bezel's steepest point).</summary>
    public Vector4 Params;

    /// <summary>x = specular intensity, y = layer index, z = max refraction magnitude (the CPU-
    /// side normalization constant for this shape's profile), w = blur radius in pixels (the
    /// "frost" -- 0 for clear glass).</summary>
    public Vector4 Params2;

    /// <summary>rgb = tint color, a = tint amount: an ordinary alpha blend over the refracted
    /// view, so 1 = a fully opaque fill (Apple's resting track/thumb) and ~0.1 = a hint of tint
    /// on clear glass.</summary>
    public Vector4 Tint;

    /// <summary>x = chromatic aberration as a fraction of the displacement (spread between the
    /// red and blue channels, ~0.1), y = shadow strength (0..1), z = shadow radius px, w = shadow
    /// y-offset px. The shadow darkens the layer beneath around the shape's outline.</summary>
    public Vector4 Extra;

    /// <summary>x = magnify: the whole shape samples toward its own centre by this fraction of
    /// the offset (0.2 = a 1.25x magnifier), so a lens overhanging an opaque track still shows
    /// the track out to its rim -- iOS 26's pressed switch thumb. y/z/w reserved.</summary>
    public Vector4 Params3;

    public static GlassShape Create(
        Vector2 center, Vector2 halfSize, float cornerRadius, float bezelWidth,
        GlassBezelProfile profile, float refractionScale, float specularIntensity, int layer,
        Vector3 tintColor, float tintAmount, float blurRadius = 0f,
        float chromatic = 0f, float shadowStrength = 0f, float shadowRadius = 0f, float shadowOffsetY = 0f,
        float magnify = 0f)
    {
        return new GlassShape
        {
            CenterHalfSize = new Vector4(center, halfSize.X, halfSize.Y),
            Params = new Vector4(cornerRadius, bezelWidth, (float)profile, refractionScale),
            Params2 = new Vector4(specularIntensity, layer, BezelProfileMath.MaxRefractionMagnitude(profile), blurRadius),
            Tint = new Vector4(tintColor, tintAmount),
            Extra = new Vector4(chromatic, shadowStrength, shadowRadius, shadowOffsetY),
            Params3 = new Vector4(magnify, 0f, 0f, 0f),
        };
    }
}

/// <summary>
/// Where overlay controls publish their glass geometry for the renderer to pick up each frame.
/// Each publisher owns a keyed slot (so a control re-publishing on every animation tick replaces
/// its own shapes rather than accumulating); the renderer takes a snapshot per frame. Static and
/// lock-guarded because publishers run on the WinUI thread and the render tick on the glass
/// window's own message loop thread.
/// </summary>
internal static class GlassShapeRegistry
{
    private static readonly object Gate = new();
    private static readonly Dictionary<object, GlassShape[]> Slots = new();
    private static GlassShape[] _snapshot = Array.Empty<GlassShape>();
    private static bool _dirty;

    public const int MaxShapes = 16;

    public static void Publish(object owner, params GlassShape[] shapes)
    {
        lock (Gate)
        {
            Slots[owner] = shapes;
            _dirty = true;
        }
    }

    public static void Remove(object owner)
    {
        lock (Gate)
        {
            if (Slots.Remove(owner))
            {
                _dirty = true;
            }
        }
    }

    /// <summary>Panel first (layer 0), then controls in publish order, truncated to MaxShapes.</summary>
    public static GlassShape[] Snapshot()
    {
        lock (Gate)
        {
            if (_dirty)
            {
                _snapshot = Slots.Values.SelectMany(s => s).OrderBy(s => s.Params2.Y).Take(MaxShapes).ToArray();
                _dirty = false;
            }
            return _snapshot;
        }
    }
}
