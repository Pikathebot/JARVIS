// Did the capture under the window change since the host last rendered? Drawn over the window's
// crop with no render target bound inside an occlusion query: a texel identical to the reference
// is discarded, so the query's sample count is the number of changed pixels.
Texture2D Current : register(t0);    // the live capture, whole display
Texture2D Reference : register(t1);  // the window's crop, as of the last render

cbuffer ChangeDetectConstants : register(b0)
{
    int2 CropOrigin; // the crop's top-left in capture pixels
    int2 Pad;
};

struct VSOutput
{
    float4 Position : SV_POSITION;
    float2 Uv : TEXCOORD0;
};

VSOutput VSMain(uint id : SV_VertexID)
{
    VSOutput o;
    o.Uv = float2((id << 1) & 2, id & 2);
    o.Position = float4(o.Uv * float2(2, -2) + float2(-1, 1), 0, 1);
    return o;
}

float4 PSMain(VSOutput input) : SV_TARGET
{
    int2 p = int2(input.Position.xy);
    if (all(Current.Load(int3(p + CropOrigin, 0)) == Reference.Load(int3(p, 0))))
    {
        discard;
    }
    return 0;
}
