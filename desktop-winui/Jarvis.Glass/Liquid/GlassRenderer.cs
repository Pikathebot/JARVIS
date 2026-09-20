using System.Numerics;
using Vortice.Direct3D;
using Vortice.Direct3D11;
using Vortice.DXGI;

namespace Jarvis_Glass;

/// <summary>
/// Layered three-pass bezel glass over a list of <see cref="GlassShape"/>s. For each layer
/// (0 = panel, then controls, back to front): pass 1 (DisplacementField.hlsl) builds a per-pixel
/// displacement field for that layer's shapes into an R16G16B16A16_FLOAT target; pass 2
/// (Refraction.hlsl) samples the layer's source through it -- the live capture (cropped to this
/// window's screen rect) for layer 0, the previous layer's finished output for every layer above;
/// pass 3 (SpecularRim.hlsl) screen-blends a Fresnel-style rim highlight and per-shape tint on
/// top (SrcBlend=One, DestBlend=InvSrcColor: result = src + dst - src*dst). Intermediate layers
/// ping-pong between two window-sized textures; the last layer draws into the swapchain.
/// When a layer carries a frosted shape, a separable Gaussian pre-pass (GaussianBlur.hlsl, H then
/// V through two scratch textures) blurs that layer's source once at the largest frost radius on
/// the layer, and pass 2 samples the blurred copy for frosted pixels.
/// Text a control publishes through <see cref="GlassScene"/> is rasterised by
/// <see cref="GlassContentSurface"/> into a per-layer texture and source-over composited onto the
/// layer right after its rim pass (Composite.hlsl), so the layers above refract it.
/// </summary>
internal sealed class GlassRenderer : IDisposable
{
    private readonly ID3D11Device _device;
    private readonly ID3D11DeviceContext _context;

    private readonly ID3D11VertexShader _fullscreenVs;
    private readonly ID3D11PixelShader _displacementPs;
    private readonly ID3D11PixelShader _refractionPs;
    private readonly ID3D11PixelShader _specularRimPs;
    private readonly ID3D11PixelShader _blurPs;
    private readonly ID3D11PixelShader _compositePs;
    private readonly GlassContentSurface _content;

    private readonly ID3D11SamplerState _pointSampler;
    private readonly ID3D11SamplerState _linearSampler;

    private readonly ID3D11Buffer _displacementConstants;
    private readonly ID3D11Buffer _refractionConstants;
    private readonly ID3D11Buffer _specularRimConstants;
    private readonly ID3D11Buffer _shapeConstants;
    private readonly ID3D11Buffer _blurConstants;

    /// <summary>float4 header + MaxShapes * GlassShape.Size.</summary>
    private const int ShapeConstantsSize = 16 + GlassScene.MaxShapes * GlassShape.Size;

    // One finished output texture per layer, kept between frames: a layer is only re-rendered
    // when its own shapes/text changed or the layer beneath it was re-rendered, so a thumb
    // animating on layer 4 costs layer 4 alone, not the pane and slabs under it.
    private readonly List<ID3D11Texture2D?> _layerTextures = new();
    private readonly List<ID3D11RenderTargetView?> _layerRtvs = new();
    private readonly List<ID3D11ShaderResourceView?> _layerSrvs = new();
    private readonly List<LayerCache> _layerCache = new();
    private GlassText[] _lastTexts = Array.Empty<GlassText>();
    private int _lastCaptureVersion = -1;
    private Vector4 _lastUvRect;

    private sealed class LayerCache
    {
        public GlassShape[] Shapes = Array.Empty<GlassShape>();
        public GlassText[] Texts = Array.Empty<GlassText>();
        public bool Valid;
    }

    // Frost scratch textures, one pair per downscale factor (layers pick 1x/2x/4x by sigma, so
    // keying by factor keeps them from being rebuilt every frame): [0] = horizontal pass output,
    // [1] = final blurred source for the refraction pass.
    private sealed class BlurTargets
    {
        public readonly ID3D11Texture2D?[] Textures = new ID3D11Texture2D?[2];
        public readonly ID3D11RenderTargetView?[] Rtvs = new ID3D11RenderTargetView?[2];
        public readonly ID3D11ShaderResourceView?[] Srvs = new ID3D11ShaderResourceView?[2];
        public int Width, Height;
    }
    private readonly Dictionary<int, BlurTargets> _blur = new();

    /// <summary>Gaussian sigma as a fraction of a shape's blur radius. 0.5 puts the kernel's 2-sigma
    /// extent at the radius, so "blur 10" spreads about as far as the old 10px Poisson disc did.</summary>
    public float BlurSigmaPerRadius { get; set; } = 0.5f;

    private readonly ID3D11BlendState _screenBlendState;
    private readonly ID3D11BlendState _overBlendState;
    private readonly ID3D11RasterizerState _scissorRasterizer;

    /// <summary>Bezel profile of the panel shape (shape 0). Tunable live from ControlOverlayWindow.</summary>
    public GlassBezelProfile Profile { get; set; } = GlassBezelProfile.Lip;

    private ID3D11Texture2D? _displacementTexture;
    private ID3D11RenderTargetView? _displacementRtv;
    private ID3D11ShaderResourceView? _displacementSrv;
    private int _fieldWidth;
    private int _fieldHeight;

    /// <summary>Panel-shape material, tunable at runtime from the overlay's sliders. Controls
    /// carry their own parameters in the shapes they publish.</summary>
    public float CornerRadius { get; set; } = 16f;
    public float BezelWidth { get; set; } = 16f;
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
        var blurPath = Path.Combine(shaderDir, "GaussianBlur.hlsl");
        var compositePath = Path.Combine(shaderDir, "Composite.hlsl");

        // All three passes draw the same full-screen triangle, so one VS compiled from
        // DisplacementField.hlsl covers all of them - the other two shader files only supply a
        // pixel shader.
        _fullscreenVs = ShaderPipeline.CreateVertexShader(_device, displacementPath, "VSMain");
        _displacementPs = ShaderPipeline.CreatePixelShader(_device, displacementPath, "PSMain");
        _refractionPs = ShaderPipeline.CreatePixelShader(_device, refractionPath, "PSMain");
        _specularRimPs = ShaderPipeline.CreatePixelShader(_device, specularRimPath, "PSMain");
        _blurPs = ShaderPipeline.CreatePixelShader(_device, blurPath, "PSMain");
        _compositePs = ShaderPipeline.CreatePixelShader(_device, compositePath, "PSMain");
        _content = new GlassContentSurface(_device);

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

        // Premultiplied source-over for the text content textures.
        var overBlendDesc = BlendDescription.Opaque;
        overBlendDesc.RenderTarget[0] = new RenderTargetBlendDescription
        {
            BlendEnable = true,
            SourceBlend = Blend.One,
            DestinationBlend = Blend.InverseSourceAlpha,
            BlendOperation = BlendOperation.Add,
            SourceBlendAlpha = Blend.One,
            DestinationBlendAlpha = Blend.InverseSourceAlpha,
            BlendOperationAlpha = BlendOperation.Add,
            RenderTargetWriteMask = ColorWriteEnable.All,
        };
        _overBlendState = _device.CreateBlendState(overBlendDesc);

        // Every pass is a full-screen triangle; the scissor is what confines a control layer's
        // passes to the pixels its shapes can actually touch (see LayerScissor).
        var rasterizerDesc = new RasterizerDescription(CullMode.None, FillMode.Solid)
        {
            ScissorEnable = true,
            DepthClipEnable = true,
        };
        _scissorRasterizer = _device.CreateRasterizerState(rasterizerDesc);

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
        // float4 UvRect (16) + float2 WindowSize + float SourceIsCapture + float Sigma (16)
        // + float2 Direction + float2 pad (16) = 48 bytes.
        _blurConstants = ShaderPipeline.CreateConstantBuffer(_device, 48);
    }

    /// <summary>The panel slab covering the whole window, built from the tunable properties above.</summary>
    public GlassShape PanelShape(int width, int height) => GlassShape.Create(
        new Vector2(width * 0.5f, height * 0.5f), new Vector2(width * 0.5f, height * 0.5f),
        CornerRadius, BezelWidth, Profile, RefractionScale, SpecularIntensity, layer: 0,
        tintColor: Vector3.One, tintAmount: 0f, secondLight: 0.33f); // the card keeps its old one-sided sheen

    private void EnsureLayerTargets(int width, int height, int layerCount)
    {
        var resized = _fieldWidth != width || _fieldHeight != height;
        if (resized)
        {
            foreach (var c in _layerCache) c.Valid = false;
        }
        while (_layerTextures.Count < layerCount)
        {
            _layerTextures.Add(null); _layerRtvs.Add(null); _layerSrvs.Add(null); _layerCache.Add(new LayerCache());
        }
        for (var i = 0; i < layerCount; i++)
        {
            if (resized || _layerTextures[i] is null)
            {
                ID3D11Texture2D? t = _layerTextures[i]; ID3D11RenderTargetView? r = _layerRtvs[i]; ID3D11ShaderResourceView? v = _layerSrvs[i];
                CreateWindowTexture(width, height, ref t, ref r, ref v);
                _layerTextures[i] = t; _layerRtvs[i] = r; _layerSrvs[i] = v;
                _layerCache[i].Valid = false;
            }
        }
        // Layers that dropped out (a lifted thumb's layer once it settles) must re-render if
        // they come back.
        for (var i = layerCount; i < _layerCache.Count; i++) _layerCache[i].Valid = false;
    }

    private int BlurDownscale(float maxBlur)
    {
        var sigma = Math.Max(0.25f, maxBlur * BlurSigmaPerRadius);
        return sigma >= 8f ? 4 : sigma >= 4f ? 2 : 1;
    }

    private BlurTargets EnsureBlurTargets(int ds, int width, int height)
    {
        if (!_blur.TryGetValue(ds, out var set))
        {
            set = new BlurTargets();
            _blur[ds] = set;
        }
        if (set.Textures[0] is not null && set.Width == width && set.Height == height) return set;
        for (var i = 0; i < 2; i++)
        {
            CreateWindowTexture(width, height, ref set.Textures[i], ref set.Rtvs[i], ref set.Srvs[i]);
        }
        set.Width = width;
        set.Height = height;
        return set;
    }

    private void CreateWindowTexture(int width, int height, ref ID3D11Texture2D? texture, ref ID3D11RenderTargetView? rtv, ref ID3D11ShaderResourceView? srv)
    {
        srv?.Dispose();
        rtv?.Dispose();
        texture?.Dispose();
        texture = _device.CreateTexture2D(new Texture2DDescription
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
        rtv = _device.CreateRenderTargetView(texture);
        srv = _device.CreateShaderResourceView(texture);
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

    /// <param name="captureVersion">Changes whenever <paramref name="captureSrv"/>'s content did;
    /// with an unchanged capture, unchanged layers are served from their cached output.</param>
    public void Draw(ID3D11RenderTargetView backBufferRtv, ID3D11ShaderResourceView captureSrv, int captureVersion, Vector4 uvRect, int width, int height, ReadOnlySpan<GlassShape> shapes, ReadOnlySpan<GlassText> texts)
    {
        // D2D first: it clobbers pipeline state, and the content textures must be finished before
        // any layer composites them. Only when the text actually changed (or the size did).
        var resized = _fieldWidth != width || _fieldHeight != height;
        if (resized || !texts.SequenceEqual(_lastTexts))
        {
            _content.Draw(width, height, texts);
            _lastTexts = texts.ToArray();
        }

        var layerCount = Math.Max(1, _content.MaxLayer + 1);
        foreach (var shape in shapes)
        {
            layerCount = Math.Max(layerCount, (int)shape.Params2.Y + 1);
        }

        // EnsureLayerTargets keys off the same size fields EnsureDisplacementTarget updates, so it
        // must run first.
        EnsureLayerTargets(width, height, layerCount);
        EnsureDisplacementTarget(width, height);

        var captureChanged = captureVersion != _lastCaptureVersion || uvRect != _lastUvRect;
        _lastCaptureVersion = captureVersion;
        _lastUvRect = uvRect;

        _context.IASetPrimitiveTopology(PrimitiveTopology.TriangleList);
        _context.IASetInputLayout(null);
        _context.VSSetShader(_fullscreenVs);
        _context.RSSetViewport(0, 0, width, height);
        _context.RSSetState(_scissorRasterizer);
        _context.PSSetSampler(0, _pointSampler);
        _context.PSSetSampler(1, _linearSampler);
        WriteDisplacementConstants(width, height);
        WriteSpecularRimConstants(width, height);

        var belowChanged = captureChanged;
        for (var layer = 0; layer < layerCount; layer++)
        {
            var target = _layerRtvs[layer]!;
            var source = layer == 0 ? captureSrv : _layerSrvs[layer - 1]!;

            var cache = _layerCache[layer];
            var layerShapes = ShapesOn(shapes, layer);
            var layerTexts = TextsOn(texts, layer);
            var same = cache.Valid && !belowChanged
                && System.Runtime.InteropServices.MemoryMarshal.AsBytes(layerShapes.AsSpan()).SequenceEqual(System.Runtime.InteropServices.MemoryMarshal.AsBytes(cache.Shapes.AsSpan()))
                && layerTexts.AsSpan().SequenceEqual(cache.Texts);
            if (same)
            {
                belowChanged = false;
                continue;
            }
            cache.Shapes = layerShapes;
            cache.Texts = layerTexts;
            cache.Valid = true;
            belowChanged = true;

            WriteShapeConstants(layerShapes, layer);
            _context.OMSetBlendState(null);

            // Layer 0 (the whole-window pane) is full-screen. Any layer above it only differs
            // from its source inside its shapes' footprints (plus bezel, refraction reach,
            // shadow and frost), so the source is copied across as-is and every pass on this
            // layer is scissored to that footprint -- a thumb layer is a few thousand pixels,
            // not the whole window, and the shaders loop over the layer's shapes per pixel.
            var maxBlur = MaxBlurOnLayer(layerShapes);
            if (layer == 0)
            {
                _context.RSSetScissorRect(0, 0, width, height);
            }
            else
            {
                var scissor = LayerScissor(layerShapes, layerTexts, maxBlur, width, height);
                using (var targetResource = target.Resource)
                using (var sourceResource = source.Resource)
                {
                    _context.CopyResource(targetResource, sourceResource);
                }
                if (scissor.Width <= 0 || scissor.Height <= 0) continue; // nothing on this layer
                _context.RSSetScissorRect(scissor.X, scissor.Y, scissor.Width, scissor.Height);
            }

            // Pass 0 (only if something on this layer is frosted): separable Gaussian of the
            // source at the layer's largest blur radius, H into scratch then V into the blurred
            // texture. Pass 2 lerps toward it per shape.
            if (maxBlur > 0f && layerShapes.Length > 0)
            {
                var sigma = Math.Max(0.25f, maxBlur * BlurSigmaPerRadius);
                // Frost is low-frequency by definition, so the Gaussian runs at reduced
                // resolution: a wide sigma at full res is 2x47 taps over every pixel per capture
                // frame, the same blur at 1/4 res and sigma/4 is 2x12 taps over 1/16 the pixels,
                // and the linear upsample in the refraction pass hides the difference.
                var ds = BlurDownscale(maxBlur);
                var bw = Math.Max(1, (width + ds - 1) / ds);
                var bh = Math.Max(1, (height + ds - 1) / ds);
                var blur = EnsureBlurTargets(ds, bw, bh);
                _context.RSSetViewport(0, 0, bw, bh);
                if (layer == 0) _context.RSSetScissorRect(0, 0, bw, bh);
                else
                {
                    var sc = LayerScissor(layerShapes, layerTexts, maxBlur, width, height);
                    _context.RSSetScissorRect(sc.X / ds, sc.Y / ds, Math.Min(bw, sc.Width / ds + 2), Math.Min(bh, sc.Height / ds + 2));
                }
                _context.PSSetShader(_blurPs);
                _context.PSSetConstantBuffer(0, _blurConstants);
                _context.PSSetShaderResource(0, null!);
                _context.PSSetShaderResource(2, null!);

                WriteBlurConstants(uvRect, bw, bh, sourceIsCapture: layer == 0, sigma / ds, horizontal: true);
                _context.OMSetRenderTargets(blur.Rtvs[0]!);
                _context.PSSetShaderResource(1, source);
                _context.Draw(3, 0);

                WriteBlurConstants(uvRect, bw, bh, sourceIsCapture: false, sigma / ds, horizontal: false);
                _context.OMSetRenderTargets(blur.Rtvs[1]!);
                _context.PSSetShaderResource(1, blur.Srvs[0]!);
                _context.Draw(3, 0);
                _context.OMSetRenderTargets((ID3D11RenderTargetView)null!);

                _context.RSSetViewport(0, 0, width, height);
                if (layer == 0) _context.RSSetScissorRect(0, 0, width, height);
                else
                {
                    var sc = LayerScissor(layerShapes, layerTexts, maxBlur, width, height);
                    _context.RSSetScissorRect(sc.X, sc.Y, sc.Width, sc.Height);
                }
            }

            // Pass 1: displacement field for this layer's shapes. Unbind the SRVs first so the
            // displacement texture (read by the previous layer's pass 2) can be a render target
            // again without a hazard warning.
            _context.PSSetShaderResource(0, null!);
            _context.PSSetShaderResource(1, null!);
            _context.OMSetRenderTargets(_displacementRtv!);
            _context.PSSetShader(_displacementPs);
            _context.PSSetConstantBuffer(0, _displacementConstants);
            _context.PSSetConstantBuffer(1, _shapeConstants);
            _context.Draw(3, 0);

            // Pass 2: refract this layer's source through the field into the layer target.
            WriteRefractionConstants(uvRect, width, height, sourceIsCapture: layer == 0, maxBlur);
            _context.OMSetRenderTargets(target);
            _context.PSSetShader(_refractionPs);
            _context.PSSetConstantBuffer(0, _refractionConstants);
            _context.PSSetConstantBuffer(1, _shapeConstants);
            _context.PSSetShaderResource(0, _displacementSrv!);
            _context.PSSetShaderResource(1, source);
            _context.PSSetShaderResource(2, (maxBlur > 0f ? _blur[BlurDownscale(maxBlur)].Srvs[1] : null)!);
            _context.Draw(3, 0);
            _context.PSSetShaderResource(2, null!);

            // Pass 3: rim highlight + tint, screen-blended onto the same target.
            _context.OMSetBlendState(_screenBlendState);
            _context.PSSetShader(_specularRimPs);
            _context.PSSetConstantBuffer(0, _specularRimConstants);
            _context.PSSetConstantBuffer(1, _shapeConstants);
            _context.Draw(3, 0);
            _context.OMSetBlendState(null);

            // Pass 4 (only if a control put text on this layer): source-over the content texture.
            if (_content.HasContent(layer))
            {
                _context.OMSetBlendState(_overBlendState);
                _context.PSSetShader(_compositePs);
                _context.PSSetShaderResource(1, _content.Srv(layer)!);
                _context.Draw(3, 0);
                _context.OMSetBlendState(null);
                _context.PSSetShaderResource(1, null!);
            }

            // The layer target becomes the next layer's source; release it as a render target.
            _context.OMSetRenderTargets((ID3D11RenderTargetView)null!);
        }

        // The top layer's output is the frame; the swapchain's back buffer changes every present
        // so it is always filled from the kept texture (a copy, not a re-render).
        using (var backBuffer = backBufferRtv.Resource)
        {
            _context.CopyResource(backBuffer, _layerTextures[layerCount - 1]!);
        }
    }

    private static GlassShape[] ShapesOn(ReadOnlySpan<GlassShape> shapes, int layer)
    {
        var count = 0;
        foreach (var shape in shapes) if ((int)shape.Params2.Y == layer) count++;
        if (count == 0) return Array.Empty<GlassShape>();
        var result = new GlassShape[count];
        var i = 0;
        foreach (var shape in shapes) if ((int)shape.Params2.Y == layer) result[i++] = shape;
        return result;
    }

    private static GlassText[] TextsOn(ReadOnlySpan<GlassText> texts, int layer)
    {
        var count = 0;
        foreach (var text in texts) if (text.Layer == layer) count++;
        if (count == 0) return Array.Empty<GlassText>();
        var result = new GlassText[count];
        var i = 0;
        foreach (var text in texts) if (text.Layer == layer) result[i++] = text;
        return result;
    }

    private void WriteDisplacementConstants(int width, int height)
    {
        Span<float> data = stackalloc float[4];
        data[0] = width;
        data[1] = height;
        WriteConstantBuffer(_displacementConstants, data);
    }

    private void WriteRefractionConstants(Vector4 uvRect, int width, int height, bool sourceIsCapture, float maxBlur)
    {
        Span<float> data = stackalloc float[8];
        data[0] = uvRect.X;
        data[1] = uvRect.Y;
        data[2] = uvRect.Z;
        data[3] = uvRect.W;
        data[4] = width;
        data[5] = height;
        data[6] = sourceIsCapture ? 1f : 0f;
        data[7] = maxBlur;
        WriteConstantBuffer(_refractionConstants, data);
    }

    private void WriteBlurConstants(Vector4 uvRect, int width, int height, bool sourceIsCapture, float sigma, bool horizontal)
    {
        Span<float> data = stackalloc float[12];
        data[0] = uvRect.X;
        data[1] = uvRect.Y;
        data[2] = uvRect.Z;
        data[3] = uvRect.W;
        data[4] = width;
        data[5] = height;
        data[6] = sourceIsCapture ? 1f : 0f;
        data[7] = sigma;
        data[8] = horizontal ? 1f : 0f;
        data[9] = horizontal ? 0f : 1f;
        WriteConstantBuffer(_blurConstants, data);
    }

    private static float MaxBlurOnLayer(ReadOnlySpan<GlassShape> layerShapes)
    {
        var max = 0f;
        foreach (var shape in layerShapes) max = Math.Max(max, shape.Params2.W);
        return max;
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

    /// <summary>Uploads one layer's shapes (the shaders' per-pixel loops run to the uploaded
    /// count, so a thumb layer costs two iterations, not 48).</summary>
    private unsafe void WriteShapeConstants(ReadOnlySpan<GlassShape> layerShapes, int activeLayer)
    {
        var count = Math.Min(layerShapes.Length, GlassScene.MaxShapes);
        var mapped = _context.Map(_shapeConstants, MapMode.WriteDiscard);
        var header = (float*)mapped.DataPointer;
        header[0] = count;
        header[1] = activeLayer;
        header[2] = 0f;
        header[3] = 0f;
        layerShapes[..count].CopyTo(new Span<GlassShape>((byte*)mapped.DataPointer + 16, count));
        _context.Unmap(_shapeConstants, 0);
    }

    /// <summary>Union of everything a control layer can write, in window pixels: each shape's
    /// rect (cut to its clip) padded by its bezel, refraction reach, shadow and frost, plus a
    /// generous box around each text run on the layer. Empty when the layer has nothing.</summary>
    private static (int X, int Y, int Width, int Height) LayerScissor(ReadOnlySpan<GlassShape> shapes, ReadOnlySpan<GlassText> texts, float maxBlur, int width, int height)
    {
        float minX = float.MaxValue, minY = float.MaxValue, maxX = float.MinValue, maxY = float.MinValue;
        var any = false;

        static void Include(ref float minX, ref float minY, ref float maxX, ref float maxY, float x0, float y0, float x1, float y1)
        {
            minX = Math.Min(minX, x0); minY = Math.Min(minY, y0);
            maxX = Math.Max(maxX, x1); maxY = Math.Max(maxY, y1);
        }

        foreach (var shape in shapes)
        {
            var c = shape.CenterHalfSize;
            var x0 = c.X - c.Z; var y0 = c.Y - c.W; var x1 = c.X + c.Z; var y1 = c.Y + c.W;
            if (shape.Clip.Z > 0f)
            {
                x0 = Math.Max(x0, shape.Clip.X); y0 = Math.Max(y0, shape.Clip.Y);
                x1 = Math.Min(x1, shape.Clip.X + shape.Clip.Z); y1 = Math.Min(y1, shape.Clip.Y + shape.Clip.W);
                if (x1 <= x0 || y1 <= y0) continue;
            }
            // Bezel + displacement reach + shadow radius and offset + frost kernel, and slack.
            var pad = shape.Params.Y + shape.Params.W + shape.Extra.Z + Math.Abs(shape.Extra.W) + 3f * maxBlur + 8f;
            Include(ref minX, ref minY, ref maxX, ref maxY, x0 - pad, y0 - pad, x1 + pad, y1 + pad);
            any = true;
        }
        foreach (var text in texts)
        {
            var halfW = text.Text.Length * text.FontSize * 0.75f + 8f;
            var halfH = text.FontSize * 1.5f + 8f;
            Include(ref minX, ref minY, ref maxX, ref maxY, text.Center.X - halfW, text.Center.Y - halfH, text.Center.X + halfW, text.Center.Y + halfH);
            any = true;
        }
        if (!any) return (0, 0, 0, 0);

        var ix0 = Math.Clamp((int)MathF.Floor(minX), 0, width);
        var iy0 = Math.Clamp((int)MathF.Floor(minY), 0, height);
        var ix1 = Math.Clamp((int)MathF.Ceiling(maxX), 0, width);
        var iy1 = Math.Clamp((int)MathF.Ceiling(maxY), 0, height);
        return (ix0, iy0, ix1 - ix0, iy1 - iy0);
    }

    private unsafe void WriteConstantBuffer(ID3D11Buffer buffer, ReadOnlySpan<float> data)
    {
        var mapped = _context.Map(buffer, MapMode.WriteDiscard);
        data.CopyTo(new Span<float>((void*)mapped.DataPointer, data.Length));
        _context.Unmap(buffer, 0);
    }

    public void Dispose()
    {
        _scissorRasterizer.Dispose();
        _displacementSrv?.Dispose();
        _displacementRtv?.Dispose();
        _displacementTexture?.Dispose();
        for (var i = 0; i < _layerTextures.Count; i++)
        {
            _layerSrvs[i]?.Dispose();
            _layerRtvs[i]?.Dispose();
            _layerTextures[i]?.Dispose();
        }
        foreach (var set in _blur.Values)
        {
            for (var i = 0; i < 2; i++)
            {
                set.Srvs[i]?.Dispose();
                set.Rtvs[i]?.Dispose();
                set.Textures[i]?.Dispose();
            }
        }
        _content.Dispose();
        _overBlendState.Dispose();
        _compositePs.Dispose();
        _screenBlendState.Dispose();
        _blurConstants.Dispose();
        _shapeConstants.Dispose();
        _specularRimConstants.Dispose();
        _refractionConstants.Dispose();
        _displacementConstants.Dispose();
        _linearSampler.Dispose();
        _pointSampler.Dispose();
        _blurPs.Dispose();
        _specularRimPs.Dispose();
        _refractionPs.Dispose();
        _displacementPs.Dispose();
        _fullscreenVs.Dispose();
    }
}
