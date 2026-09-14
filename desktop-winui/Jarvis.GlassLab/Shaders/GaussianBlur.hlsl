// Pre-pass (per layer, only when a shape on it carries frost): a separable Gaussian over the
// layer's source, run twice -- horizontally from the source into a scratch texture, then
// vertically from the scratch into the blurred texture Refraction.hlsl samples for frosted
// pixels. Replaces the 12-tap Poisson disc that used to sit inside SampleSource: at the card's
// radius 10 that disc read as a dozen faint ghost copies of the desktop rather than a frost, and
// its cost scaled with every chromatic channel sampled through it.
//
// The horizontal pass is the only place the capture's UvRect crop is applied, so both blur
// outputs are already window-space (identity UVs), like every layer texture above layer 0.
//
// Sigma is in window pixels; taps extend 3 sigma each side, capped so the loop stays bounded.
// Weights are evaluated in-shader (one exp per tap) rather than uploaded -- the radius is a
// live-tunable and this pass runs on a 900x600 window, not a 4K frame.

cbuffer BlurConstants : register(b0)
{
    float4 UvRect;         // xy = UV offset, zw = UV scale into the capture (horizontal pass only)
    float2 WindowSize;     // pixels
    float SourceIsCapture; // 1 = crop via UvRect, 0 = source is already window-space
    float Sigma;           // pixels
    float2 Direction;      // (1,0) horizontal, (0,1) vertical
    float2 _Pad0;
};

#define MAX_TAPS 48

Texture2D SourceTexture : register(t1);
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
    float sigma = max(Sigma, 0.25);
    int taps = min(MAX_TAPS, (int)ceil(sigma * 3.0));
    float2 stepUv = Direction / WindowSize;
    float invTwoSigmaSq = 1.0 / (2.0 * sigma * sigma);

    float4 sum = SourceTexture.SampleLevel(LinearSampler, ToSourceUv(i.Uv), 0);
    float weightSum = 1.0;
    [loop]
    for (int k = 1; k <= MAX_TAPS; k++)
    {
        if (k > taps) break;
        float w = exp(-(float)(k * k) * invTwoSigmaSq);
        float2 offset = stepUv * (float)k;
        sum += w * SourceTexture.SampleLevel(LinearSampler, ToSourceUv(i.Uv + offset), 0);
        sum += w * SourceTexture.SampleLevel(LinearSampler, ToSourceUv(i.Uv - offset), 0);
        weightSum += 2.0 * w;
    }
    return sum / weightSum;
}
