// Source-over of a premultiplied content texture (DirectWrite text drawn by GlassContentSurface)
// onto the current layer target. The blend state does the "over"; this just fetches the texel.
Texture2D ContentTexture : register(t1);
SamplerState PointSampler : register(s0);

struct VSOutput
{
    float4 Position : SV_POSITION;
    float2 Uv : TEXCOORD0;
};

float4 PSMain(VSOutput input) : SV_TARGET
{
    return ContentTexture.Sample(PointSampler, input.Uv);
}
