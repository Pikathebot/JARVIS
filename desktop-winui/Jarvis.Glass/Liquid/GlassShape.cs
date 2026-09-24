using System.Numerics;
using System.Runtime.InteropServices;

namespace Jarvis_Glass;

/// <summary>
/// One glass slab for the renderer: a rounded rect in window pixel space with its own bezel
/// material. The panel is shape 0 on layer 0; every control (a toggle's track, its thumb, a
/// slider's rail...) is one or more shapes on higher layers. Layers render back-to-front and each
/// layer refracts the *output* of the layer below it, which is what makes a thumb bend the track
/// it sits on rather than the raw desktop -- the layering is the whole point of the kube.io
/// material, not a detail.
///
/// Layout mirrors GlassShape in the HLSL cbuffer exactly (seven float4s, 112 bytes) -- a mismatch
/// here is a silent wrong-output bug, so the struct is explicit-layout and its size asserted.
/// </summary>
[StructLayout(LayoutKind.Sequential, Size = Size)]
public struct GlassShape
{
    public const int Size = 112;

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
    /// the track out to its rim -- iOS 26's pressed switch thumb. y = edge ring: a ~1px bright
    /// outline just inside the rim, independent of the light direction (Apple's "this is glass"
    /// cue at rest). z = second light weight: how much the rim on the side AWAY from the light
    /// catches an exit highlight (0 = lit from one edge only, 1 = symmetric). w = fade: 0 solid,
    /// 1 gone -- scales the shape's coverage, rim and shadow. Never set by controls; GlassScene
    /// fills it from <see cref="GlassMotion.OpacityProperty"/>.</summary>
    public Vector4 Params3;

    /// <summary>Clip rect in window px (x, y, w, h); w &lt;= 0 = unclipped. Intersected with the
    /// shape's SDF in the shaders, so a control scrolled half out of a ScrollViewer is cut at the
    /// viewport edge exactly like its XAML is.</summary>
    public Vector4 Clip;

    public static GlassShape Create(
        Vector2 center, Vector2 halfSize, float cornerRadius, float bezelWidth,
        GlassBezelProfile profile, float refractionScale, float specularIntensity, int layer,
        Vector3 tintColor, float tintAmount, float blurRadius = 0f,
        float chromatic = 0f, float shadowStrength = 0f, float shadowRadius = 0f, float shadowOffsetY = 0f,
        float magnify = 0f, float edgeRing = 0f, float secondLight = 0.6f, Vector4 clip = default)
    {
        return new GlassShape
        {
            CenterHalfSize = new Vector4(center, halfSize.X, halfSize.Y),
            Params = new Vector4(cornerRadius, bezelWidth, (float)profile, refractionScale),
            Params2 = new Vector4(specularIntensity, layer, BezelProfileMath.MaxRefractionMagnitude(profile), blurRadius),
            Tint = new Vector4(tintColor, tintAmount),
            Extra = new Vector4(chromatic, shadowStrength, shadowRadius, shadowOffsetY),
            Params3 = new Vector4(magnify, edgeRing, secondLight, 0f),
            Clip = clip,
        };
    }
}
