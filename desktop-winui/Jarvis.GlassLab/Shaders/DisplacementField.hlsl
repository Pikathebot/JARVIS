// Phase 4, pass 1: builds a per-pixel horizontal/vertical displacement field for a squircle-
// profile bezel around this window's rounded-rect edge. Ported from
// desktop-winui/Jarvis.Glass/BezelDisplacementMap.cs's CPU math (rounded-rect SDF, central-
// difference normal, Snell's-law refraction), but computed live per-pixel in-shader instead of
// via a precomputed 1D lookup + CPU-rasterized bitmap, and with the profile swapped from
// BezelDisplacementMap's convex circle to the squircle from the reference article:
//   h(t) = (1 - (1-t)^4)^(1/4)
// Output is written to an R16G16_FLOAT target (not B8G8R8A8) specifically to avoid the
// byte-order trap BezelDisplacementMap.cs hit (BGRA channel order silently swapping X/Y) - here
// R=dx, G=dy, unambiguously.

cbuffer DisplacementConstants : register(b0)
{
    float2 WindowSize;      // pixels
    float CornerRadius;     // pixels
    float BezelWidth;       // pixels
    float MaxRefractionMagnitude; // normalization constant computed on the CPU once at startup
    float3 _Pad0;
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
    o.Uv = uv;
    o.Position = float4(uv.x * 2.0 - 1.0, 1.0 - uv.y * 2.0, 0.0, 1.0);
    return o;
}

// Squircle surface height, t=0 at the outer edge, t=1 in the flat interior.
float ProfileHeight(float t)
{
    float u = 1.0 - t;
    float v = max(0.0, 1.0 - u * u * u * u);
    return pow(v, 0.25);
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

// Vector Snell's law through the squircle profile's slope at depth t, projected through the
// remaining glass thickness - same derivation as BezelDisplacementMap.BuildRefractionLookup,
// just evaluated inline instead of via a precomputed table.
float RefractionMagnitude(float t)
{
    const float eps = 0.001;
    float t0 = clamp(t - eps, 0.0, 1.0);
    float t1 = clamp(t + eps, 0.0, 1.0);
    float h0 = ProfileHeight(t0);
    float h1 = ProfileHeight(t1);
    float dhdt = (h1 - h0) / max(1e-4, t1 - t0);

    float2 normal = normalize(float2(-dhdt, -1.0));
    const float2 incident = float2(0.0, 1.0);
    const float eta = 1.0 / 1.5;

    float dotv = dot(incident, normal);
    float k = 1.0 - eta * eta * (1.0 - dotv * dotv);
    if (k < 0.0)
    {
        // Total internal reflection - not physically meaningful here, guarded to avoid NaN.
        return 0.0;
    }

    float sqrtK = sqrt(k);
    float2 refracted = eta * incident - (eta * dotv + sqrtK) * normal;
    float thickness = 1.0 - t;
    return refracted.y > 1e-4 ? (refracted.x / refracted.y) * thickness : 0.0;
}

float2 PSMain(VSOutput i) : SV_TARGET
{
    float2 pixelPos = i.Uv * WindowSize;
    float2 halfSize = WindowSize * 0.5;
    float2 p = pixelPos - halfSize;

    float r = min(CornerRadius, min(halfSize.x, halfSize.y));
    float bezel = max(1.0, min(BezelWidth, r));

    float sdf = RoundRectSdf(p, halfSize, r);
    if (sdf > 0.0 || sdf < -bezel)
    {
        // Outside the window, or past the bezel band into the flat interior: no displacement.
        return float2(0.0, 0.0);
    }

    float t = saturate(1.0 - (-sdf) / bezel); // 1 at outer edge, 0 at the bezel's inner boundary
    float mag = RefractionMagnitude(t) / MaxRefractionMagnitude;

    float2 normal = SdfGradient(p, halfSize, r);
    return -normal * mag;
}
