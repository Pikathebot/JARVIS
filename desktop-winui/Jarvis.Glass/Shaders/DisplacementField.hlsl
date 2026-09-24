// Pass 1 (per layer): builds a per-pixel displacement field for every glass shape on the active
// layer. Ported from desktop-winui/Jarvis.Glass/BezelDisplacementMap.cs's CPU math (rounded-rect
// SDF, central-difference normal, Snell's-law refraction), computed live per pixel.
//
// Output (R16G16B16A16_FLOAT): xy = displacement in *pixels* (each shape's refraction scale is
// already folded in), z = coverage (1 inside any shape on this layer, else 0), w = blur radius (px).
// Deliberately not B8G8R8A8 so there is no byte-order trap.
//
// Shapes come from ShapeConstants (b1) -- the same block SpecularRim.hlsl reads. Two bezel
// profiles per shape, matching GlassBezelProfile / BezelProfileMath.cs:
//   0 squircle: h(t) = (1 - (1-t)^4)^(1/4)              -- panels, thumbs
//   1 lip:      smootherstep blend of convex + concave  -- kube.io's switch/slider tracks
//   2 lens:     empirical ramp, see RefractionMagnitude     -- kube.io's lifted slider thumb

cbuffer DisplacementConstants : register(b0)
{
    float2 WindowSize;      // pixels
    float2 _Pad0;
};

#define MAX_SHAPES 96
#define LENS_FALLOFF 1.2 // Lens ramp exponent; 1 = linear (BezelProfileMath.LensFalloff)

struct GlassShape
{
    float4 CenterHalfSize; // xy center px, zw half size px
    float4 Params;         // x corner radius, y bezel width, z profile, w refraction scale (px)
    float4 Params2;        // x specular intensity, y layer, z max refraction magnitude, w blur radius px
    float4 Tint;           // rgb tint color, a tint amount (alpha blend)
    float4 Extra;          // x chromatic px, y shadow strength, z shadow radius px, w shadow y-offset px
    float4 Params3;        // x magnify (fraction of the centre offset the sample moves inward), w fade (0 = solid, 1 = gone)
    float4 Clip;          // x, y, w, h px; w <= 0 = unclipped
};

cbuffer ShapeConstants : register(b1)
{
    float4 Header; // x = shape count, y = active layer
    GlassShape Shapes[MAX_SHAPES];
};

struct VSOutput
{
    float4 Position : SV_POSITION;
    float2 Uv : TEXCOORD0;
};

VSOutput VSMain(uint vertexId : SV_VertexID)
{
    VSOutput o;
    float2 uv = float2((vertexId << 1) & 2, vertexId & 2);
    o.Position = float4(uv * float2(2.0, -2.0) + float2(-1.0, 1.0), 0.0, 1.0);
    o.Uv = uv;
    return o;
}

// t=0 at the outer edge, t=1 in the flat interior.
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

// Vector Snell's law through the profile's slope at depth t, projected through the remaining
// glass thickness - same derivation as BezelDisplacementMap.BuildRefractionLookup.
float RefractionMagnitude(float t, float profile)
{
    if (profile >= 1.5)
    {
        // Lens: kube.io's lifted slider thumb. Measured off their render rather than derived:
        // the bend is ~1.6 radii at the very edge and falls off almost linearly to zero at the
        // inner boundary (slightly steeper right at the edge). Read along a radius that gives:
        // a dark sliver at the edge (samples overshoot past the rail), a rail-thick strip where
        // the ramp sweeps back across the rail (mirrored, ~1x magnification), a dark gap, then
        // the undisplaced rail through the middle. Snell through any dome concentrates the bend
        // far too close to the edge for that -- the strip collapses to a 2px line.
        return pow(1.0 - t, LENS_FALLOFF);
    }

    const float eps = 0.001;
    float t0 = clamp(t - eps, 0.0, 1.0);
    float t1 = clamp(t + eps, 0.0, 1.0);
    float h0 = ProfileHeight(t0, profile);
    float h1 = ProfileHeight(t1, profile);
    float dhdt = (h1 - h0) / max(1e-4, t1 - t0);

    float2 normal = normalize(float2(-dhdt, -1.0));
    const float2 incident = float2(0.0, 1.0);
    const float eta = 1.0 / 1.5;

    float dotv = dot(incident, normal);
    float k = 1.0 - eta * eta * (1.0 - dotv * dotv);
    if (k < 0.0)
    {
        return 0.0;
    }

    float sqrtK = sqrt(k);
    float2 refracted = eta * incident - (eta * dotv + sqrtK) * normal;
    float thickness = 1.0 - t;
    return refracted.y > 1e-4 ? (refracted.x / refracted.y) * thickness : 0.0;
}

// The shape on the active layer this pixel belongs to: the one it is deepest inside of (smallest
// signed distance), so overlapping shapes on one layer resolve to the innermost. -1 if none.

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
        return float4(0.0, 0.0, 0.0, 0.0);
    }

    float blur = Shapes[s].Params2.w;
    // Fade (GlassMotion.Opacity) scales coverage: the refraction pass blends the shape over its
    // passthrough by coverage, so a half-faded shape is half glass, half what is beneath it.
    float visible = 1.0 - saturate(Shapes[s].Params3.w);
    float coverage = Coverage(sdf) * visible;
    float bezel = max(1.0, min(Shapes[s].Params.y, r));
    // Magnifier: every pixel samples toward the centre by this fraction of its offset. Applies
    // across the whole shape (interior and bezel alike) so it is continuous at the bezel's
    // inner boundary; the bezel's own profile displacement is added on top.
    float2 magnify = -p * Shapes[s].Params3.x;
    if (sdf < -bezel)
    {
        // Past the bezel band into the flat interior: covered, only the magnifier applies.
        return float4(magnify, visible, blur);
    }

    // Depth into the bezel band: 0 at the outer edge, 1 at the inner boundary -- the convention
    // the profile functions use (h(0) = 0 at the edge, flat at 1). Feeding them the inverted
    // value put the maximum bend at the INNER boundary and none at the edge, which drew a
    // sharp ring one bezel-width inside every lens.
    float u = saturate((-sdf) / bezel);
    float mag = RefractionMagnitude(u, Shapes[s].Params.z) / Shapes[s].Params2.z;
    float2 normal = SdfGradient(p, Shapes[s].CenterHalfSize.zw, r);
    // Params.w may be negative: the rim then bends OUTWARD (samples past the shape's edge),
    // which is what puts the dark band just inside the rim of iOS 26's pressed switch thumb.
    return float4(-normal * mag * Shapes[s].Params.w + magnify, coverage, blur);
}
