// Phase 4, pass 2: samples the live-capture texture offset by the displacement field from pass 1
// (DisplacementField.hlsl), cropped to this window's own screen rect via UvRect (same crop used
// by Phase 3's passthrough). Replaces Passthrough.hlsl now that there is real displacement to
// apply.

cbuffer RefractionConstants : register(b0)
{
    float4 UvRect;         // xy = UV offset, zw = UV scale - crop into the whole-display capture
    float2 WindowSize;     // pixels
    float RefractionScale; // pixels, tunable
    float _Pad0;
};

Texture2D DisplacementTexture : register(t0);
Texture2D CaptureTexture : register(t1);
SamplerState PointSampler : register(s0);
SamplerState LinearSampler : register(s1);

struct VSOutput
{
    float4 Position : SV_POSITION;
    float2 Uv : TEXCOORD0;
};

float4 PSMain(VSOutput i) : SV_TARGET
{
    float2 disp = DisplacementTexture.Sample(PointSampler, i.Uv).xy;

    // disp is normalized to roughly [-1,1]; scale to pixels, then to this window's own UV space,
    // then into the captured display's UV space via the same crop passthrough used.
    float2 windowUvDelta = (disp * RefractionScale) / WindowSize;
    float2 captureUv = UvRect.xy + (i.Uv + windowUvDelta) * UvRect.zw;

    // Force alpha=1: this pass means to fully replace what's behind the window with a distorted
    // view of it, not blend translucently - and critically, screen-capture textures typically
    // carry alpha=0, which under this swapchain's premultiplied alpha mode would otherwise
    // composite as fully transparent (i.e. invisible - indistinguishable from this pass doing
    // nothing at all, which is exactly the bug this line fixes).
    float3 color = CaptureTexture.Sample(LinearSampler, captureUv).rgb;
    return float4(color, 1.0);
}
