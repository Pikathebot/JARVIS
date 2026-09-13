using System.Numerics;
using Vortice.Direct3D;
using Vortice.Direct3D11;
using Vortice.DXGI;

namespace Jarvis_GlassLab;

/// <summary>
/// Layered three-pass bezel glass over a list of <see cref="GlassShape"/>s. For each layer
/// (0 = panel, then controls, back to front): pass 1 (DisplacementField.hlsl) builds a per-pixel
/// displacement field for that layer's shapes into an R16G16B16A16_FLOAT target; pass 2
/// (Refraction.hlsl) samples the layer's source through it -- the live capture (cropped to this
/// window's screen rect) for layer 0, the previous layer's finished output for every layer above;
/// pass 3 (SpecularRim.hlsl) screen-blends a Fresnel-style rim highlight and per-shape tint on
/// top (SrcBlend=One, DestBlend=InvSrcColor: result = src + dst - src*dst). Intermediate layers
/// ping-pong between two window-sized textures; the last layer draws into the swapchain.
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
    private readonly ID3D11Buffer _shapeConstants;

    /// <summary>float4 header + MaxShapes * GlassShape.Size.</summary>
    private const int ShapeConstantsSize = 16 + GlassShapeRegistry.MaxShapes * GlassShape.Size;

    private readonly ID3D11Texture2D?[] _layerTextures = new ID3D11Texture2D?[2];
    private readonly ID3D11RenderTargetView?[] _layerRtvs = new ID3D11RenderTargetView?[2];
    private readonly ID3D11ShaderResourceView?[] _layerSrvs = new ID3D11ShaderResourceView?[2];

    private readonly ID3D11BlendState _screenBlendState;

    /// <summary>Bezel profile of the panel shape (shape 0). Tunable live from ControlOverlayWindow.</summary>
    public GlassBezelProfile Profile { get; set; } = GlassBezelProfile.Squircle;

    private ID3D11Texture2D? _displacementTexture;
    private ID3D11RenderTargetView? _displacementRtv;
    private ID3D11ShaderResourceView? _displacementSrv;
    private int _fieldWidth;
    private int _fieldHeight;

    /// <summary>Panel-shape material, tunable at runtime from the overlay's sliders. Controls
    /// carry their own parameters in the shapes they publish.</summary>
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
            // Leave the destination's alpha alone: layer 0 is transparent outside the panel and
            // the rim pass must not turn those pixels opaque.
            SourceBlendAlpha = Blend.Zero,
            DestinationBlendAlpha = Blend.One,
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

        // float2 WindowSize + float2 pad = 16 bytes.
        _displacementConstants = ShaderPipeline.CreateConstantBuffer(_device, 16);
        // float4 UvRect (16) + float2 WindowSize + float SourceIsCapture + float pad (16) = 32 bytes.
        _refractionConstants = ShaderPipeline.CreateConstantBuffer(_device, 32);
        // float2 WindowSize + float2 pad (16) + float3 LightDir + float pad (16) = 32 bytes.
        _specularRimConstants = ShaderPipeline.CreateConstantBuffer(_device, 32);
        _shapeConstants = ShaderPipeline.CreateConstantBuffer(_device, ShapeConstantsSize);
    }

    /// <summary>The panel slab covering the whole window, built from the tunable properties above.</summary>
    public GlassShape PanelShape(int width, int height) => GlassShape.Create(
        new Vector2(width * 0.5f, height * 0.5f), new Vector2(width * 0.5f, height * 0.5f),
        CornerRadius, BezelWidth, Profile, RefractionScale, SpecularIntensity, layer: 0,
        tintColor: Vector3.One, tintAmount: 0f);

    private void EnsureLayerTargets(int width, int height)
    {
        if (_layerTextures[0] is not null && _fieldWidth == width && _fieldHeight == height)
        {
            return;
        }

        for (var i = 0; i < 2; i++)
        {
            _layerSrvs[i]?.Dispose();
            _layerRtvs[i]?.Dispose();
            _layerTextures[i]?.Dispose();
            _layerTextures[i] = _device.CreateTexture2D(new Texture2DDescription
            {
                Width = (uint)width,
                Height = (uint)height,
                MipLevels = 1,
                ArraySize = 1,
                Format = Format.B8G8R8A8_UNorm,
                SampleDescription = new SampleDescription(1, 0),
                Usage = ResourceUsage.Default,
                BindFlags = BindFlags.RenderTarget | BindFlags.ShaderResource,
            });
            _layerRtvs[i] = _device.CreateRenderTargetView(_layerTextures[i]);
            _layerSrvs[i] = _device.CreateShaderResourceView(_layerTextures[i]);
        }
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
            Format = Format.R16G16B16A16_Float,
            SampleDescription = new SampleDescription(1, 0),
            Usage = ResourceUsage.Default,
            BindFlags = BindFlags.RenderTarget | BindFlags.ShaderResource,
        });
        _displacementRtv = _device.CreateRenderTargetView(_displacementTexture);
        _displacementSrv = _device.CreateShaderResourceView(_displacementTexture);
    }

    public void Draw(ID3D11RenderTargetView backBufferRtv, ID3D11ShaderResourceView captureSrv, Vector4 uvRect, int width, int height, ReadOnlySpan<GlassShape> shapes)
    {
        // EnsureLayerTargets keys off the same size fields EnsureDisplacementTarget updates, so it
        // must run first.
        EnsureLayerTargets(width, height);
        EnsureDisplacementTarget(width, height);

        var layerCount = 1;
        foreach (var shape in shapes)
        {
            layerCount = Math.Max(layerCount, (int)shape.Params2.Y + 1);
        }

        _context.IASetPrimitiveTopology(PrimitiveTopology.TriangleList);
        _context.IASetInputLayout(null);
        _context.VSSetShader(_fullscreenVs);
        _context.RSSetViewport(0, 0, width, height);
        _context.PSSetSampler(0, _pointSampler);
        _context.PSSetSampler(1, _linearSampler);
        WriteDisplacementConstants(width, height);
        WriteSpecularRimConstants(width, height);

        for (var layer = 0; layer < layerCount; layer++)
        {
            var isLast = layer == layerCount - 1;
            var target = isLast ? backBufferRtv : _layerRtvs[layer % 2]!;
            var source = layer == 0 ? captureSrv : _layerSrvs[(layer - 1) % 2]!;

            WriteShapeConstants(shapes, layer);
            _context.OMSetBlendState(null);

            // Pass 1: displacement field for this layer's shapes. Unbind the SRVs first so the
            // displacement texture (read by the previous layer's pass 2) can be a render target
            // again without a hazard warning.
            _context.PSSetShaderResource(0, null);
            _context.PSSetShaderResource(1, null);
            _context.OMSetRenderTargets(_displacementRtv!);
            _context.PSSetShader(_displacementPs);
            _context.PSSetConstantBuffer(0, _displacementConstants);
            _context.PSSetConstantBuffer(1, _shapeConstants);
            _context.Draw(3, 0);

            // Pass 2: refract this layer's source through the field into the layer target.
            WriteRefractionConstants(uvRect, width, height, sourceIsCapture: layer == 0);
            _context.OMSetRenderTargets(target);
            _context.PSSetShader(_refractionPs);
            _context.PSSetConstantBuffer(0, _refractionConstants);
            _context.PSSetConstantBuffer(1, _shapeConstants);
            _context.PSSetShaderResource(0, _displacementSrv!);
            _context.PSSetShaderResource(1, source);
            _context.Draw(3, 0);

            // Pass 3: rim highlight + tint, screen-blended onto the same target.
            _context.OMSetBlendState(_screenBlendState);
            _context.PSSetShader(_specularRimPs);
            _context.PSSetConstantBuffer(0, _specularRimConstants);
            _context.PSSetConstantBuffer(1, _shapeConstants);
            _context.Draw(3, 0);
            _context.OMSetBlendState(null);

            // The layer target becomes the next layer's source; release it as a render target.
            _context.OMSetRenderTargets((ID3D11RenderTargetView?)null);
        }
    }

    private void WriteDisplacementConstants(int width, int height)
    {
        Span<float> data = stackalloc float[4];
        data[0] = width;
        data[1] = height;
        WriteConstantBuffer(_displacementConstants, data);
    }

    private void WriteRefractionConstants(Vector4 uvRect, int width, int height, bool sourceIsCapture)
    {
        Span<float> data = stackalloc float[8];
        data[0] = uvRect.X;
        data[1] = uvRect.Y;
        data[2] = uvRect.Z;
        data[3] = uvRect.W;
        data[4] = width;
        data[5] = height;
        data[6] = sourceIsCapture ? 1f : 0f;
        WriteConstantBuffer(_refractionConstants, data);
    }

    private void WriteSpecularRimConstants(int width, int height)
    {
        Span<float> data = stackalloc float[8];
        data[0] = width;
        data[1] = height;
        data[4] = LightDirection.X;
        data[5] = LightDirection.Y;
        data[6] = LightDirection.Z;
        WriteConstantBuffer(_specularRimConstants, data);
    }

    private unsafe void WriteShapeConstants(ReadOnlySpan<GlassShape> shapes, int activeLayer)
    {
        var count = Math.Min(shapes.Length, GlassShapeRegistry.MaxShapes);
        var mapped = _context.Map(_shapeConstants, MapMode.WriteDiscard);
        var header = (float*)mapped.DataPointer;
        header[0] = count;
        header[1] = activeLayer;
        header[2] = 0f;
        header[3] = 0f;
        var dest = new Span<GlassShape>((byte*)mapped.DataPointer + 16, count);
        shapes[..count].CopyTo(dest);
        _context.Unmap(_shapeConstants, 0);
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
        for (var i = 0; i < 2; i++)
        {
            _layerSrvs[i]?.Dispose();
            _layerRtvs[i]?.Dispose();
            _layerTextures[i]?.Dispose();
        }
        _screenBlendState.Dispose();
        _shapeConstants.Dispose();
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
