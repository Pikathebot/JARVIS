// Pass 2 (per layer): samples the layer's source through the displacement field from pass 1.
// Layer 0's source is the live-capture texture cropped to this window's screen rect via UvRect;
// every higher layer's source is the previous layer's finished output (window-space, identity
// UVs), so a control refracts the already-refracted panel beneath it rather than the raw desktop.
//
// Pixels not covered by any shape on this layer pass the source straight through -- for layer 0
// that means fully transparent (premultiplied alpha 0), so the window is see-through outside the
// panel's rounded rect instead of showing an unbent copy of the desktop there.

cbuffer RefractionConstants : register(b0)
{
    float4 UvRect;         // xy = UV offset, zw = UV scale - crop into the whole-display capture
    float2 WindowSize;     // pixels
    float SourceIsCapture; // 1 = sample the capture via UvRect, 0 = sample the previous layer 1:1
    float _Pad0;
};

Texture2D DisplacementTexture : register(t0);
Texture2D SourceTexture : register(t1);
SamplerState PointSampler : register(s0);
SamplerState LinearSampler : register(s1);

struct VSOutput
{
    float4 Position : SV_POSITION;
    float2 Uv : TEXCOORD0;
};

float2 ToSourceUv(float2 windowUv)
{
    return SourceIsCapture > 0.5 ? UvRect.xy + windowUv * UvRect.zw : windowUv;
}

float4 PSMain(VSOutput i) : SV_TARGET
{
    float4 field = DisplacementTexture.Sample(PointSampler, i.Uv);

    if (field.z < 0.5)
    {
        // Uncovered on this layer.
        if (SourceIsCapture > 0.5)
        {
            return float4(0.0, 0.0, 0.0, 0.0);
        }
        return SourceTexture.Sample(LinearSampler, i.Uv);
    }

    float2 windowUv = i.Uv + field.xy / WindowSize;
    float3 color = SourceTexture.Sample(LinearSampler, ToSourceUv(windowUv)).rgb;

    // Force alpha=1: a covered pixel fully replaces what's behind it with the refracted view --
    // and capture textures typically carry alpha=0, which under this swapchain's premultiplied
    // mode would otherwise composite as invisible. Tint/frost is applied by the rim pass, whose
    // screen blend is exactly "lerp toward a color".
    return float4(color, 1.0);
}
