// Pass 3 (per layer): specular Fresnel-style rim highlight plus per-shape tint, screen-blended
// on top of pass 2's output via the pipeline's blend state (SrcBlend=One, DestBlend=InvSrcColor:
// result = src + dst - src*dst = lerp(dst, 1, src) per channel -- so emitting tintColor*amount
// here IS "lerp toward the tint color by amount", which is why tint lives in this pass and not
// in the refraction pass). Alpha is left untouched by the blend state so uncovered (transparent)
// pixels stay transparent. Same rounded-rect SDF/bezel geometry as DisplacementField.hlsl,
// recomputed rather than threaded through the intermediate texture.

cbuffer SpecularRimConstants : register(b0)
{
    float2 WindowSize;  // pixels
    float2 _Pad0;
    float3 LightDir;    // normalized, pointing from the surface toward the light
    float _Pad1;
};

#define MAX_SHAPES 16

struct GlassShape
{
    float4 CenterHalfSize;
    float4 Params;
    float4 Params2;
    float4 Tint;
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

int FindShape(float2 pixelPos, out float outSdf, out float2 outP, out float outR)
{
    int count = (int)Header.x;
    int layer = (int)Header.y;
    int best = -1;
    float bestSdf = 1e9;
    outSdf = 0.0; outP = 0.0; outR = 0.0;
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
        // Uncovered: zero RGB under the screen blend is a no-op (result = dst).
        return float4(0.0, 0.0, 0.0, 0.0);
    }

    float3 tint = Shapes[s].Tint.rgb * Shapes[s].Tint.a;

    float bezel = max(1.0, min(Shapes[s].Params.y, r));
    if (sdf < -bezel)
    {
        // Flat interior: tint only, no rim.
        return float4(tint, 0.0);
    }

    float t = saturate(1.0 - (-sdf) / bezel); // 1 at outer edge, 0 at the bezel's inner boundary
    float profile = Shapes[s].Params.z;

    const float eps = 0.001;
    float t0 = clamp(t - eps, 0.0, 1.0);
    float t1 = clamp(t + eps, 0.0, 1.0);
    float dhdt = (ProfileHeight(t1, profile) - ProfileHeight(t0, profile)) / max(1e-4, t1 - t0);

    float2 outward2D = SdfGradient(p, Shapes[s].CenterHalfSize.zw, r);
    // A bump-like normal: tilts toward the outward direction proportional to the profile's
    // slope, mostly "up" (toward the viewer) where the profile flattens out.
    float3 normal = normalize(float3(-dhdt * outward2D.x, -dhdt * outward2D.y, 1.0));

    float rim = saturate(dot(normal, LightDir));
    rim = pow(rim, 3.0);

    // Sharp edge falloff per the kube.io reference: concentrated right at the outer edge (t=1),
    // near-zero toward the bezel's inner boundary (t=0).
    float edgeFalloff = sqrt(saturate(1.0 - (1.0 - t) * (1.0 - t)));

    float3 rimColor = float3(1.0, 1.0, 1.0) * rim * edgeFalloff * Shapes[s].Params2.x;
    return float4(saturate(rimColor + tint), 0.0);
}
