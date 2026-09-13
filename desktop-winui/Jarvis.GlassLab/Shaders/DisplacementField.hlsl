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

cbuffer DisplacementConstants : register(b0)
{
    float2 WindowSize;      // pixels
    float2 _Pad0;
};

#define MAX_SHAPES 16

struct GlassShape
{
    float4 CenterHalfSize; // xy center px, zw half size px
    float4 Params;         // x corner radius, y bezel width, z profile, w refraction scale (px)
    float4 Params2;        // x specular intensity, y layer, z max refraction magnitude, w blur radius px
    float4 Tint;           // rgb tint color, a tint amount (alpha blend)
    float4 Extra;          // x chromatic px, y shadow strength, z shadow radius px, w shadow y-offset px
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
    return profile >= 0.5 ? LipHeight(t) : SquircleHeight(t);
}

float RoundRectSdf(float2 p, float2 halfSize, float r)
{
    float2 q = abs(p) - (halfSize - r);
    float outsideLen = length(max(q, float2(0.0, 0.0)));
    return outsideLen + min(max(q.x, q.y), 0.0) - r;
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
        float sdf = RoundRectSdf(p, halfSize, r);
        if (sdf <= 0.0 && sdf < bestSdf)
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
    float bezel = max(1.0, min(Shapes[s].Params.y, r));
    if (sdf < -bezel)
    {
        // Past the bezel band into the flat interior: covered, undisplaced.
        return float4(0.0, 0.0, 1.0, blur);
    }

    float t = saturate(1.0 - (-sdf) / bezel); // 1 at outer edge, 0 at the bezel's inner boundary
    float mag = RefractionMagnitude(t, Shapes[s].Params.z) / Shapes[s].Params2.z;
    float2 normal = SdfGradient(p, Shapes[s].CenterHalfSize.zw, r);
    return float4(-normal * mag * Shapes[s].Params.w, 1.0, blur);
}
