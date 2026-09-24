// Pass 3 (per layer): specular Fresnel-style rim highlight, screen-blended on top of pass 2's
// output via the pipeline's blend state (SrcBlend=One, DestBlend=InvSrcColor:
// result = src + dst - src*dst). Alpha is left untouched by the blend state so uncovered
// (transparent) pixels stay transparent. Same rounded-rect SDF/bezel geometry as
// DisplacementField.hlsl, recomputed rather than threaded through the intermediate texture.
// (Tint is an alpha blend in Refraction.hlsl -- a screen blend can only brighten, which cannot
// produce Apple's opaque green/grey track over a bright backdrop.)

cbuffer SpecularRimConstants : register(b0)
{
    float2 WindowSize;  // pixels
    float2 _Pad0;
    float3 LightDir;    // normalized, pointing from the surface toward the light
    float _Pad1;
};

#define MAX_SHAPES 96

struct GlassShape
{
    float4 CenterHalfSize;
    float4 Params;
    float4 Params2;
    float4 Tint;
    float4 Extra;
    float4 Params3;        // x magnify, y edge ring intensity, z second (exit) light weight, w fade
    float4 Clip;           // x, y, w, h px; w <= 0 = unclipped
};

cbuffer ShapeConstants : register(b1)
{
    float4 Header;
    GlassShape Shapes[MAX_SHAPES];
};

struct VSOutput
{
    float4 Position : SV_POSITION;
    float2 Uv : TEXCOORD0;
};

float SquircleHeight(float t)
{
    float u = 1.0 - t;
    float v = max(0.0, 1.0 - u * u * u * u);
    return pow(v, 0.25);
}

float ConvexCircleHeight(float t)
{
    float u = 1.0 - t;
    return sqrt(max(0.0, 1.0 - u * u));
}

float ConcaveHeight(float t)
{
    return 1.0 - sqrt(max(0.0, 1.0 - t * t));
}

float Smootherstep(float t)
{
    t = saturate(t);
    return t * t * t * (t * (t * 6.0 - 15.0) + 10.0);
}

float LipHeight(float t)
{
    float convex = ConvexCircleHeight(t);
    float concave = ConcaveHeight(t);
    float w = Smootherstep(t);
    return convex * (1.0 - w) + concave * w;
}

float ProfileHeight(float t, float profile)
{
    if (profile >= 1.5) return ConvexCircleHeight(t);
    return profile >= 0.5 ? LipHeight(t) : SquircleHeight(t);
}

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

float2 SdfGradient(float2 p, float2 halfSize, float r)
{
    const float eps = 0.75;
    float d0 = RoundRectSdf(p - float2(eps, 0.0), halfSize, r);
    float d1 = RoundRectSdf(p + float2(eps, 0.0), halfSize, r);
    float d2 = RoundRectSdf(p - float2(0.0, eps), halfSize, r);
    float d3 = RoundRectSdf(p + float2(0.0, eps), halfSize, r);
    float2 g = float2(d1 - d0, d3 - d2);
    float len = length(g);
    return len > 1e-6 ? g / len : float2(0.0, -1.0);
}


// 1px anti-aliased coverage from the signed distance: 1 inside, 0 half a pixel outside the
// outline, linear across the edge pixel. The hard sdf <= 0 test drew every puck and pill with
// stair-stepped edges (the slider thumb read as an octagon).
float Coverage(float sdf)
{
    return saturate(0.5 - sdf);
}

int FindShape(float2 pixelPos, out float outSdf, out float2 outP, out float outR)
{
    int count = (int)Header.x;
    int layer = (int)Header.y;
    int best = -1;
    float bestSdf = 1e9;
    outSdf = 0.0; outP = 0.0; outR = 0.0;
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
            bestSdf = sdf; best = s; outSdf = sdf; outP = p; outR = r;
        }
    }
    return best;
}

float4 PSMain(VSOutput i) : SV_TARGET
{
    float2 pixelPos = i.Uv * WindowSize;

    float sdf; float2 p; float r;
    int s = FindShape(pixelPos, sdf, p, r);
    if (s < 0)
    {
        // Uncovered: zero RGB under the screen blend is a no-op (result = dst).
        return float4(0.0, 0.0, 0.0, 0.0);
    }

    float bezel = max(1.0, min(Shapes[s].Params.y, r));
    if (sdf < -bezel)
    {
        // Flat interior: no rim.
        return float4(0.0, 0.0, 0.0, 0.0);
    }

    float u = saturate((-sdf) / bezel); // 0 at outer edge, 1 at the bezel's inner boundary (profile convention)
    float t = 1.0 - u;                   // 1 at outer edge, for the edge falloff below
    float profile = Shapes[s].Params.z;

    const float eps = 0.001;
    float u0 = clamp(u - eps, 0.0, 1.0);
    float u1 = clamp(u + eps, 0.0, 1.0);
    float dhdt = (ProfileHeight(u1, profile) - ProfileHeight(u0, profile)) / max(1e-4, u1 - u0);

    float2 outward2D = SdfGradient(p, Shapes[s].CenterHalfSize.zw, r);
    // A bump-like normal: tilts toward the outward direction proportional to the profile's
    // slope, mostly "up" (toward the viewer) where the profile flattens out.
    float3 normal = normalize(float3(-dhdt * outward2D.x, -dhdt * outward2D.y, 1.0));

    // Directional: only the bezel's tilt toward the light counts. Using dot(normal, LightDir)
    // directly lit the whole band ~uniformly (LightDir's z alone gives every near-flat pixel
    // ~0.7), which read as a grey translucent border around a lifted thumb. A faint counter-
    // highlight on the far side keeps the lens from looking lit from one edge only.
    // "facing" is by the OUTWARD direction, not the bump normal: the entry lobe sits on the
    // corner arc that points toward the light (upper-left), the exit lobe on the one pointing
    // away (lower-right) -- the normal's inward tilt at a convex edge would swap them.
    float tilt = length(normal.xy);
    float2 l2 = normalize(LightDir.xy);
    float facing = dot(outward2D, l2);
    // Two lobes, not an outline: the sheen lives on the corner arcs that face the light (entry,
    // upper-left) and the ones facing away (exit, lower-right, Params3.z as strong -- Apple's
    // slabs are lit two-sided), and the straight runs between them fade to a faint floor. A flat
    // base term here (it used to be 0.55) lights the whole outline evenly, which reads as a
    // drawn border, not a lit edge. Confined to the outer sliver of the band by a steep falloff.
    float secondLight = Shapes[s].Params3.z;
    float lobes = pow(saturate(facing), 1.5) + secondLight * pow(saturate(-facing), 1.5);
    const float floorLight = 0.08;
    float rim = floorLight + (1.0 - floorLight) * lobes;
    rim *= saturate(tilt * 3.0);
    float edgeFalloff = pow(t, 8.0);
    float coverage = Coverage(sdf) * (1.0 - saturate(Shapes[s].Params3.w)); // fade: GlassMotion.Opacity
    float3 rimColor = float3(1.0, 1.0, 1.0) * rim * edgeFalloff * Shapes[s].Params2.x * coverage;

    // Edge ring: a ~1px bright line just inside the outline, independent of the bezel profile
    // (so it shows even when the band is flat), weighted by the same lobes so it too is a
    // lit edge rather than a border.
    float ring = smoothstep(1.5, 0.5, abs(sdf + 1.0)) * Shapes[s].Params3.y * coverage
               * (floorLight + (1.0 - floorLight) * lobes);
    rimColor += ring;
    return float4(saturate(rimColor), 0.0);
}
