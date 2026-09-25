// Pass 2 (per layer): samples the layer's source through the displacement field from pass 1.
// Layer 0's source is the live-capture texture cropped to this window's screen rect via UvRect;
// every higher layer's source is the previous layer's finished output (window-space, identity
// UVs), so a control refracts the already-refracted panel beneath it rather than the raw desktop.
//
// Also owns everything that needs the shape table per pixel and is not a brighten-only effect:
//   - tint: an ordinary alpha blend of the shape's color over the refracted view (1 = opaque
//     fill, which is what Apple's resting track and thumb are; the rim pass's screen blend
//     could only ever brighten);
//   - chromatic aberration: red and blue sampled with slightly different displacement scales at
//     the rim, the rainbow fringe on Apple's pressed thumb;
//   - shadow: for pixels NOT covered on this layer, darken the passthrough source near a shape
//     that carries a shadow, offset downward -- what lifts the thumb off the track.
//
// All texture reads are SampleLevel (mip 0): plain Sample needs screen-space derivatives, which
// forces fxc to fully unroll every loop the call sits under -- with the 16-shape loops that
// explodes into a compile that never finishes (it hung the app at startup once).
//
// Pixels not covered by any shape on this layer pass the source straight through -- for layer 0
// that means fully transparent (premultiplied alpha 0), so the window is see-through outside the
// panel's rounded rect instead of showing an unbent copy of the desktop there.

cbuffer RefractionConstants : register(b0)
{
    float4 UvRect;         // xy = UV offset, zw = UV scale - crop into the whole-display capture
    float2 WindowSize;     // pixels
    float SourceIsCapture; // 1 = sample the capture via UvRect, 0 = sample the previous layer 1:1
    float MaxBlur;         // px: the blur radius BlurredTexture was built with (0 = not built)
};

#define MAX_SHAPES 256

struct GlassShape
{
    float4 CenterHalfSize;
    float4 Params;
    float4 Params2;
    float4 Tint;
    float4 Extra;
    float4 Params3;        // w = fade (0 = solid, 1 = gone); coverage already carries it (field.z)
    float4 Clip;           // x, y, w, h px; w <= 0 = unclipped
};

cbuffer ShapeConstants : register(b1)
{
    float4 Header;
    GlassShape Shapes[MAX_SHAPES];
};

Texture2D DisplacementTexture : register(t0);
Texture2D SourceTexture : register(t1);
// The source pre-blurred by GaussianBlur.hlsl at MaxBlur (the largest frost on this layer);
// always window-space. Only bound when MaxBlur > 0.
Texture2D BlurredTexture : register(t2);
SamplerState PointSampler : register(s0);
SamplerState LinearSampler : register(s1);

struct VSOutput
{
    float4 Position : SV_POSITION;
    float2 Uv : TEXCOORD0;
};

float RoundRectSdf(float2 p, float2 halfSize, float r)
{
    float2 q = abs(p) - (halfSize - r);
    float outsideLen = length(max(q, float2(0.0, 0.0)));
    return outsideLen + min(max(q.x, q.y), 0.0) - r;
}

// Intersects a shape's SDF with its clip rect (Clip = x, y, w, h in px; w <= 0 = none): the
// max of two SDFs is their intersection, so coverage, bezel and rim all stop at the clip edge.
float ClipSdf(float2 pixelPos, float4 clip)
{
    if (clip.z <= 0.0) return -1e9;
    float2 c = clip.xy + clip.zw * 0.5;
    float2 q = abs(pixelPos - c) - clip.zw * 0.5;
    return length(max(q, 0.0)) + min(max(q.x, q.y), 0.0);
}

int FindShape(float2 pixelPos, out float outSdf)
{
    int count = (int)Header.x;
    int layer = (int)Header.y;
    int best = -1;
    float bestSdf = 1e9;
    outSdf = 0.0;
    [loop]
    for (int s = 0; s < MAX_SHAPES; s++)
    {
        if (s >= count) break;
        if ((int)Shapes[s].Params2.y != layer) continue;

        float2 halfSize = Shapes[s].CenterHalfSize.zw;
        float2 p = pixelPos - Shapes[s].CenterHalfSize.xy;
        float r = min(Shapes[s].Params.x, min(halfSize.x, halfSize.y));
        float sdf = max(RoundRectSdf(p, halfSize, r), ClipSdf(pixelPos, Shapes[s].Clip));
        if (sdf <= 0.5 && sdf < bestSdf) // half a pixel outside too: see Coverage()
        {
            bestSdf = sdf; best = s; outSdf = sdf;
        }
    }
    return best;
}

// Total darkening at an uncovered pixel from every shadow-casting shape on this layer.
float ShadowAt(float2 pixelPos)
{
    int count = (int)Header.x;
    int layer = (int)Header.y;
    float shade = 0.0;
    [loop]
    for (int s = 0; s < MAX_SHAPES; s++)
    {
        if (s >= count) break;
        if ((int)Shapes[s].Params2.y != layer) continue;
        float strength = Shapes[s].Extra.y * (1.0 - saturate(Shapes[s].Params3.w)); // fades with the shape
        float radius = Shapes[s].Extra.z;
        if (strength <= 0.0 || radius <= 0.0) continue;

        float2 halfSize = Shapes[s].CenterHalfSize.zw;
        float2 p = pixelPos - Shapes[s].CenterHalfSize.xy - float2(0.0, Shapes[s].Extra.w);
        float r = min(Shapes[s].Params.x, min(halfSize.x, halfSize.y));
        float sdf = max(RoundRectSdf(p, halfSize, r), ClipSdf(pixelPos - float2(0.0, Shapes[s].Extra.w), Shapes[s].Clip));
        // Soft falloff from the (offset) outline outward; smootherstep so it has no hard ring.
        float t = saturate(1.0 - max(sdf, 0.0) / radius);
        float fall = t * t * t * (t * (t * 6.0 - 15.0) + 10.0);
        shade = max(shade, strength * fall);
    }
    return shade;
}

float2 ToSourceUv(float2 windowUv)
{
    return SourceIsCapture > 0.5 ? UvRect.xy + windowUv * UvRect.zw : windowUv;
}

float3 SampleSource(float2 windowUv, float blur)
{
    float3 sharp = SourceTexture.SampleLevel(LinearSampler, ToSourceUv(windowUv), 0).rgb;
    if (blur < 0.5 || MaxBlur <= 0.0)
    {
        return sharp;
    }

    // Frost: the separable Gaussian pre-pass blurred this layer's source at the layer's largest
    // frost radius. A shape wanting less than that gets a blend toward the sharp sample -- not a
    // true smaller kernel, but every frosted shape on a layer so far shares one radius anyway.
    float3 frosted = BlurredTexture.SampleLevel(LinearSampler, windowUv, 0).rgb;
    return lerp(sharp, frosted, saturate(blur / MaxBlur));
}

float4 PSMain(VSOutput i) : SV_TARGET
{
    float4 field = DisplacementTexture.SampleLevel(PointSampler, i.Uv, 0);
    float2 pixelPos = i.Uv * WindowSize;

    // field.z is the shape's anti-aliased coverage (see DisplacementField.hlsl Coverage): the
    // passthrough is what shows through the fractional edge pixel, so it's computed whenever
    // coverage is less than 1, not only for fully uncovered pixels.
    float coverage = field.z;
    float4 through = float4(0.0, 0.0, 0.0, 0.0);
    if (coverage < 1.0 && SourceIsCapture < 0.5)
    {
        through = SourceTexture.SampleLevel(LinearSampler, i.Uv, 0);
        through.rgb *= 1.0 - ShadowAt(pixelPos);
    }
    if (coverage <= 0.0)
    {
        return through;
    }

    float sdf;
    int s = FindShape(pixelPos, sdf);
    float blur = field.w;
    float chromatic = s >= 0 ? Shapes[s].Extra.x : 0.0;

    float2 dispUv = field.xy / WindowSize;
    float3 color;
    if (chromatic > 0.001 && dot(field.xy, field.xy) > 0.0)
    {
        // Spread the channels along the displacement, proportional to it (chromatic is a
        // fraction of the displacement): red bends least, blue most. A constant-width spread
        // switched on wherever displacement is non-zero drew a fringe ring at the bezel's inner
        // boundary, where displacement is ~0 but the spread suddenly was not.
        float2 dir = field.xy * chromatic / WindowSize;
        color.r = SampleSource(i.Uv + dispUv - dir, blur).r;
        color.g = SampleSource(i.Uv + dispUv, blur).g;
        color.b = SampleSource(i.Uv + dispUv + dir, blur).b;
    }
    else
    {
        color = SampleSource(i.Uv + dispUv, blur);
    }

    if (s >= 0)
    {
        color = lerp(color, Shapes[s].Tint.rgb, Shapes[s].Tint.a);
    }

    // Force alpha=1: a covered pixel fully replaces what's behind it -- and capture textures
    // typically carry alpha=0, which under this swapchain's premultiplied mode would otherwise
    // composite as invisible. Blending by coverage against the (premultiplied) passthrough is
    // what anti-aliases the outline; on layer 0 the passthrough is transparent, so edge pixels
    // come out as premultiplied partial alpha.
    return lerp(through, float4(color, 1.0), coverage);
}
