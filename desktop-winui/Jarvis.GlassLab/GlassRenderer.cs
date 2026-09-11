using System.Numerics;
using Vortice.Direct3D;
using Vortice.Direct3D11;
using Vortice.DXGI;

namespace Jarvis_GlassLab;

/// <summary>
/// Phase 5: three-pass squircle-bezel glass. Pass 1 (DisplacementField.hlsl) builds a per-pixel
/// displacement field into an intermediate R16G16_FLOAT target sized to the window; pass 2
/// (Refraction.hlsl) samples the live-capture texture offset by that field, cropped to this
/// window's screen rect, into the swapchain back buffer; pass 3 (SpecularRim.hlsl) adds a
/// Fresnel-style rim highlight on top, screen-blended via a fixed-function blend state
/// (SrcBlend=One, DestBlend=InvSrcColor - D3D11 has no native "screen" blend mode, so this is the
/// standard trick: result = src + dst - src*dst).
/// </summary>
internal sealed class GlassRenderer : IDisposable
{
    private readonly ID3D11Device _device;
    private readonly ID3D11DeviceContext _context;

    private readonly ID3D11VertexShader _fullscreenVs;
    private readonly ID3D11PixelShader _displacementPs;
    private readonly ID3D11PixelShader _refractionPs;
    private readonly ID3D11PixelShader _specularRimPs;

    private readonly ID3D11SamplerState _pointSampler;
    private readonly ID3D11SamplerState _linearSampler;

    private readonly ID3D11Buffer _displacementConstants;
    private readonly ID3D11Buffer _refractionConstants;
    private readonly ID3D11Buffer _specularRimConstants;

    private readonly ID3D11BlendState _screenBlendState;

    private readonly float _maxRefractionMagnitude;

    private ID3D11Texture2D? _displacementTexture;
    private ID3D11RenderTargetView? _displacementRtv;
    private ID3D11ShaderResourceView? _displacementSrv;
    private int _fieldWidth;
    private int _fieldHeight;

    /// <summary>Tunable at runtime for visual iteration - not yet wired to any UI, just fields to
    /// poke from App while eyeballing the result.</summary>
    public float CornerRadius { get; set; } = 48f;
    public float BezelWidth { get; set; } = 36f;
    public float RefractionScale { get; set; } = 28f;

    /// <summary>Fixed light direction (normalized), pointing from the glass surface toward the
    /// light - upper-left and tilted toward the viewer, a common convention for a glossy
    /// highlight. Not mouse-reactive yet (deferred per the plan).</summary>
    public Vector3 LightDirection { get; set; } = Vector3.Normalize(new Vector3(-0.4f, -0.6f, 0.7f));
    public float SpecularIntensity { get; set; } = 1.2f;

    public GlassRenderer(ID3D11Device device, ID3D11DeviceContext context, string shaderDir)
    {
        _device = device;
        _context = context;

        var displacementPath = Path.Combine(shaderDir, "DisplacementField.hlsl");
        var refractionPath = Path.Combine(shaderDir, "Refraction.hlsl");
        var specularRimPath = Path.Combine(shaderDir, "SpecularRim.hlsl");

        // All three passes draw the same full-screen triangle, so one VS compiled from
        // DisplacementField.hlsl covers all of them - the other two shader files only supply a
        // pixel shader.
        _fullscreenVs = ShaderPipeline.CreateVertexShader(_device, displacementPath, "VSMain");
        _displacementPs = ShaderPipeline.CreatePixelShader(_device, displacementPath, "PSMain");
        _refractionPs = ShaderPipeline.CreatePixelShader(_device, refractionPath, "PSMain");
        _specularRimPs = ShaderPipeline.CreatePixelShader(_device, specularRimPath, "PSMain");

        var screenBlendDesc = BlendDescription.Opaque;
        screenBlendDesc.RenderTarget[0] = new RenderTargetBlendDescription
        {
            BlendEnable = true,
            SourceBlend = Blend.One,
            DestinationBlend = Blend.InverseSourceColor,
            BlendOperation = BlendOperation.Add,
            SourceBlendAlpha = Blend.One,
            DestinationBlendAlpha = Blend.Zero,
            BlendOperationAlpha = BlendOperation.Add,
            RenderTargetWriteMask = ColorWriteEnable.All,
        };
        _screenBlendState = _device.CreateBlendState(screenBlendDesc);

        _pointSampler = _device.CreateSamplerState(new SamplerDescription
        {
            Filter = Filter.MinMagMipPoint,
            AddressU = TextureAddressMode.Clamp,
            AddressV = TextureAddressMode.Clamp,
            AddressW = TextureAddressMode.Clamp,
            ComparisonFunc = ComparisonFunction.Never,
        });
        _linearSampler = _device.CreateSamplerState(new SamplerDescription
        {
            Filter = Filter.MinMagMipLinear,
            AddressU = TextureAddressMode.Clamp,
            AddressV = TextureAddressMode.Clamp,
            AddressW = TextureAddressMode.Clamp,
            ComparisonFunc = ComparisonFunction.Never,
        });

        // float2 WindowSize + float CornerRadius + float BezelWidth (16) + float MaxRefractionMagnitude + float3 pad (16) = 32 bytes.
        _displacementConstants = ShaderPipeline.CreateConstantBuffer(_device, 32);
        // float4 UvRect (16) + float2 WindowSize + float RefractionScale + float pad (16) = 32 bytes.
        _refractionConstants = ShaderPipeline.CreateConstantBuffer(_device, 32);
        // float2 WindowSize + float CornerRadius + float BezelWidth (16) + float3 LightDir + float Intensity (16) = 32 bytes.
        _specularRimConstants = ShaderPipeline.CreateConstantBuffer(_device, 32);

        _maxRefractionMagnitude = SquircleProfile.ComputeMaxRefractionMagnitude();
    }

    private void EnsureDisplacementTarget(int width, int height)
    {
        if (_displacementTexture is not null && _fieldWidth == width && _fieldHeight == height)
        {
            return;
        }

        _fieldWidth = width;
        _fieldHeight = height;

        _displacementSrv?.Dispose();
        _displacementRtv?.Dispose();
        _displacementTexture?.Dispose();

        _displacementTexture = _device.CreateTexture2D(new Texture2DDescription
        {
            Width = (uint)width,
            Height = (uint)height,
            MipLevels = 1,
            ArraySize = 1,
            Format = Format.R16G16_Float,
            SampleDescription = new SampleDescription(1, 0),
            Usage = ResourceUsage.Default,
            BindFlags = BindFlags.RenderTarget | BindFlags.ShaderResource,
        });
        _displacementRtv = _device.CreateRenderTargetView(_displacementTexture);
        _displacementSrv = _device.CreateShaderResourceView(_displacementTexture);
    }

    public void Draw(ID3D11RenderTargetView backBufferRtv, ID3D11ShaderResourceView captureSrv, Vector4 uvRect, int width, int height)
    {
        EnsureDisplacementTarget(width, height);

        _context.IASetPrimitiveTopology(PrimitiveTopology.TriangleList);
        _context.IASetInputLayout(null);
        _context.VSSetShader(_fullscreenVs);
        _context.RSSetViewport(0, 0, width, height);
        _context.OMSetBlendState(null); // default opaque - reset in case pass 3 left the screen blend state active from last frame

        // Pass 1: displacement field.
        WriteDisplacementConstants(width, height);
        _context.OMSetRenderTargets(_displacementRtv!);
        _context.PSSetShader(_displacementPs);
        _context.PSSetConstantBuffer(0, _displacementConstants);
        _context.Draw(3, 0);

        // Pass 2: sample live capture through the displacement field, into the swapchain.
        WriteRefractionConstants(uvRect, width, height);
        _context.OMSetRenderTargets(backBufferRtv);
        _context.PSSetShader(_refractionPs);
        _context.PSSetConstantBuffer(0, _refractionConstants);
        _context.PSSetShaderResource(0, _displacementSrv!);
        _context.PSSetShaderResource(1, captureSrv);
        _context.PSSetSampler(0, _pointSampler);
        _context.PSSetSampler(1, _linearSampler);
        _context.Draw(3, 0);

        // Pass 3: specular rim highlight, screen-blended on top of pass 2's output - same render
        // target, no need to change OMSetRenderTargets, just the blend state and shader.
        WriteSpecularRimConstants(width, height);
        _context.OMSetBlendState(_screenBlendState);
        _context.PSSetShader(_specularRimPs);
        _context.PSSetConstantBuffer(0, _specularRimConstants);
        _context.Draw(3, 0);
        _context.OMSetBlendState(null);
    }

    private void WriteDisplacementConstants(int width, int height)
    {
        Span<float> data = stackalloc float[8];
        data[0] = width;
        data[1] = height;
        data[2] = CornerRadius;
        data[3] = BezelWidth;
        data[4] = _maxRefractionMagnitude;
        WriteConstantBuffer(_displacementConstants, data);
    }

    private void WriteRefractionConstants(Vector4 uvRect, int width, int height)
    {
        Span<float> data = stackalloc float[8];
        data[0] = uvRect.X;
        data[1] = uvRect.Y;
        data[2] = uvRect.Z;
        data[3] = uvRect.W;
        data[4] = width;
        data[5] = height;
        data[6] = RefractionScale;
        WriteConstantBuffer(_refractionConstants, data);
    }

    private void WriteSpecularRimConstants(int width, int height)
    {
        Span<float> data = stackalloc float[8];
        data[0] = width;
        data[1] = height;
        data[2] = CornerRadius;
        data[3] = BezelWidth;
        data[4] = LightDirection.X;
        data[5] = LightDirection.Y;
        data[6] = LightDirection.Z;
        data[7] = SpecularIntensity;
        WriteConstantBuffer(_specularRimConstants, data);
    }

    private unsafe void WriteConstantBuffer(ID3D11Buffer buffer, ReadOnlySpan<float> data)
    {
        var mapped = _context.Map(buffer, MapMode.WriteDiscard);
        data.CopyTo(new Span<float>((void*)mapped.DataPointer, data.Length));
        _context.Unmap(buffer, 0);
    }

    public void Dispose()
    {
        _displacementSrv?.Dispose();
        _displacementRtv?.Dispose();
        _displacementTexture?.Dispose();
        _screenBlendState.Dispose();
        _specularRimConstants.Dispose();
        _refractionConstants.Dispose();
        _displacementConstants.Dispose();
        _linearSampler.Dispose();
        _pointSampler.Dispose();
        _specularRimPs.Dispose();
        _refractionPs.Dispose();
        _displacementPs.Dispose();
        _fullscreenVs.Dispose();
    }
}
