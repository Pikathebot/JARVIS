// Phase 5, pass 3: a specular Fresnel-style rim highlight, additively screen-blended on top of
// pass 2's refracted output via the pipeline's blend state (SrcBlend=One, DestBlend=InvSrcColor -
// the standard fixed-function trick for "screen" blending: result = src + dst - src*dst - since
// D3D11 has no native screen blend mode). Reuses the same rounded-rect SDF/bezel-band geometry as
// DisplacementField.hlsl, recomputed here rather than threaded through the intermediate texture,
// per the plan's own note that recomputing this cheaply beats widening that texture's format.

cbuffer SpecularRimConstants : register(b0)
{
    float2 WindowSize;  // pixels
    float CornerRadius; // pixels
    float BezelWidth;   // pixels
    float3 LightDir;    // normalized, pointing from the surface toward the light
    float Intensity;
};

struct VSOutput
{
    float4 Position : SV_POSITION;
    float2 Uv : TEXCOORD0;
};

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

float4 PSMain(VSOutput i) : SV_TARGET
{
    float2 pixelPos = i.Uv * WindowSize;
    float2 halfSize = WindowSize * 0.5;
    float2 p = pixelPos - halfSize;

    float r = min(CornerRadius, min(halfSize.x, halfSize.y));
    float bezel = max(1.0, min(BezelWidth, r));

    float sdf = RoundRectSdf(p, halfSize, r);
    if (sdf > 0.0 || sdf < -bezel)
    {
        // Outside the window, or in the flat interior - no rim contribution. Returning zero RGB
        // with the screen blend state below is a no-op (src=0 -> result=dst unchanged).
        return float4(0.0, 0.0, 0.0, 1.0);
    }

    float t = saturate(1.0 - (-sdf) / bezel); // 1 at outer edge, 0 at the bezel's inner boundary

    const float eps = 0.001;
    float t0 = clamp(t - eps, 0.0, 1.0);
    float t1 = clamp(t + eps, 0.0, 1.0);
    float dhdt = (ProfileHeight(t1) - ProfileHeight(t0)) / max(1e-4, t1 - t0);

    float2 outward2D = SdfGradient(p, halfSize, r);
    // A bump-like normal: tilts toward the outward direction proportional to the profile's
    // slope, mostly "up" (toward the viewer) where the profile flattens out.
    float3 normal = normalize(float3(-dhdt * outward2D.x, -dhdt * outward2D.y, 1.0));

    float rim = saturate(dot(normal, LightDir));
    rim = pow(rim, 3.0);

    // Sharp edge falloff per the kube.io reference: concentrated right at the outer edge (t=1),
    // near-zero toward the bezel's inner boundary (t=0) - keeps the highlight a thin rim, not a
    // wash across the whole bezel band.
    float edgeFalloff = sqrt(saturate(1.0 - (1.0 - t) * (1.0 - t)));

    float3 rimColor = float3(1.0, 1.0, 1.0) * rim * edgeFalloff * Intensity;
    return float4(rimColor, 1.0);
}
