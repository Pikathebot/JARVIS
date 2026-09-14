using System.Numerics;
using Vortice.DCommon;
using Vortice.Direct2D1;
using Vortice.Direct3D11;
using Vortice.DirectWrite;
using Vortice.DXGI;
using Vortice.Mathematics;
using D2DAlphaMode = Vortice.DCommon.AlphaMode;

namespace Jarvis_Glass;

/// <summary>
/// Rasterises the frame's <see cref="GlassText"/> items with Direct2D/DirectWrite into one
/// window-sized premultiplied BGRA texture per glass layer that carries text. Each texture is a
/// plain D3D11 render target the renderer composites (source-over) right after that layer's
/// passes and that the next layer then refracts as part of its source. D2D draws through the same
/// D3D11 device (BgraSupport is already on for DirectComposition), so no copies or shared
/// handles -- but D2D leaves the D3D pipeline state as it pleases, so <see cref="Draw"/> must run
/// before the renderer binds anything for its own passes.
/// </summary>
internal sealed class GlassContentSurface : IDisposable
{
    private sealed class LayerSurface : IDisposable
    {
        public ID3D11Texture2D Texture = null!;
        public ID3D11ShaderResourceView Srv = null!;
        public ID2D1RenderTarget Target = null!;
        public ID2D1SolidColorBrush Brush = null!;
        public bool HasContent;

        public void Dispose()
        {
            Brush?.Dispose();
            Target?.Dispose();
            Srv?.Dispose();
            Texture?.Dispose();
        }
    }

    /// <summary>WinUI's default face. DirectWrite falls back per glyph if a family is missing.</summary>
    public const string FontFamily = "Segoe UI Variable Text";

    /// <summary>Vertical nudge, in em, that moves DirectWrite's line-box centre onto the cap
    /// height centre -- the equivalent of XAML's TextLineBounds="Tight" so caption text sits
    /// optically centred in its pill rather than a hair high. (ascent-descent)/2 - capHeight/2
    /// for Segoe UI: (1.079-0.25)/2 - 0.35.</summary>
    public const float TightNudgeEm = 0.065f;

    private readonly ID3D11Device _device;
    private readonly ID2D1Factory1 _d2dFactory;
    private readonly IDWriteFactory _dwriteFactory;
    private readonly Dictionary<int, LayerSurface> _layers = new();
    private readonly Dictionary<(int size10, int weight), IDWriteTextFormat> _formats = new();
    private int _width, _height;

    public GlassContentSurface(ID3D11Device device)
    {
        _device = device;
        _d2dFactory = D2D1.D2D1CreateFactory<ID2D1Factory1>(Vortice.Direct2D1.FactoryType.SingleThreaded, DebugLevel.None);
        _dwriteFactory = DWrite.DWriteCreateFactory<IDWriteFactory>(Vortice.DirectWrite.FactoryType.Shared);
    }

    /// <summary>True when the last <see cref="Draw"/> put anything on this layer.</summary>
    public bool HasContent(int layer) => _layers.TryGetValue(layer, out var s) && s.HasContent;

    public ID3D11ShaderResourceView? Srv(int layer) => _layers.TryGetValue(layer, out var s) ? s.Srv : null;

    /// <summary>Highest layer index that has text this frame, or -1.</summary>
    public int MaxLayer { get; private set; } = -1;

    /// <summary>Redraws every layer surface for this frame: layers with text are cleared and
    /// drawn, layers that had text last frame but not now are cleared once and marked empty.</summary>
    public void Draw(int width, int height, ReadOnlySpan<GlassText> texts)
    {
        if (width != _width || height != _height)
        {
            foreach (var s in _layers.Values) s.Dispose();
            _layers.Clear();
            _width = width;
            _height = height;
        }

        foreach (var s in _layers.Values) s.HasContent = false;
        MaxLayer = -1;
        foreach (var t in texts)
        {
            if (string.IsNullOrEmpty(t.Text)) continue;
            var s = GetOrCreate(t.Layer);
            s.HasContent = true;
            MaxLayer = Math.Max(MaxLayer, t.Layer);
        }

        foreach (var (layer, s) in _layers)
        {
            s.Target.BeginDraw();
            s.Target.Clear(new Color4(0f, 0f, 0f, 0f));
            if (s.HasContent)
            {
                foreach (var t in texts)
                {
                    if (t.Layer != layer || string.IsNullOrEmpty(t.Text)) continue;
                    DrawText(s, t);
                }
            }
            s.Target.EndDraw();
        }
    }

    private void DrawText(LayerSurface s, in GlassText t)
    {
        var format = GetFormat(t.FontSize, t.FontWeight);
        // Premultiplied target, straight colour in: D2D's brush takes straight RGBA and does the
        // multiply itself.
        s.Brush.Color = new Color4(t.Color.X, t.Color.Y, t.Color.Z, t.Color.W);
        // Generous layout box centred on the requested point; the format centres both axes inside
        // it, and the nudge lands the cap height (not the line box) on Center.Y.
        const float boxW = 4096f, boxH = 512f;
        var y = t.Center.Y + t.FontSize * TightNudgeEm;
        var rect = new Rect(t.Center.X - boxW * 0.5f, y - boxH * 0.5f, boxW, boxH);
        var clipped = t.Clip.Z > 0f;
        if (clipped) s.Target.PushAxisAlignedClip(new Rect(t.Clip.X, t.Clip.Y, t.Clip.Z, t.Clip.W), Vortice.Direct2D1.AntialiasMode.PerPrimitive);
        s.Target.DrawText(t.Text, format, rect, s.Brush, DrawTextOptions.None, MeasuringMode.Natural);
        if (clipped) s.Target.PopAxisAlignedClip();
    }

    private IDWriteTextFormat GetFormat(float size, int weight)
    {
        var key = ((int)MathF.Round(size * 10f), weight);
        if (_formats.TryGetValue(key, out var format)) return format;
        format = _dwriteFactory.CreateTextFormat(FontFamily, null, (FontWeight)weight, FontStyle.Normal, FontStretch.Normal, key.Item1 / 10f, "en-us");
        format.TextAlignment = TextAlignment.Center;
        format.ParagraphAlignment = ParagraphAlignment.Center;
        format.WordWrapping = WordWrapping.NoWrap;
        _formats[key] = format;
        return format;
    }

    private LayerSurface GetOrCreate(int layer)
    {
        if (_layers.TryGetValue(layer, out var s)) return s;
        s = new LayerSurface();
        s.Texture = _device.CreateTexture2D(new Texture2DDescription
        {
            Width = (uint)_width,
            Height = (uint)_height,
            MipLevels = 1,
            ArraySize = 1,
            Format = Format.B8G8R8A8_UNorm,
            SampleDescription = new SampleDescription(1, 0),
            Usage = ResourceUsage.Default,
            BindFlags = BindFlags.RenderTarget | BindFlags.ShaderResource,
        });
        s.Srv = _device.CreateShaderResourceView(s.Texture);
        using var surface = s.Texture.QueryInterface<IDXGISurface>();
        // 96 DPI so one D2D DIP is one texel: all GlassText geometry is already in physical px.
        var props = new RenderTargetProperties(RenderTargetType.Default,
            new PixelFormat(Format.B8G8R8A8_UNorm, D2DAlphaMode.Premultiplied), 96f, 96f,
            RenderTargetUsage.None, Vortice.Direct2D1.FeatureLevel.Default);
        s.Target = _d2dFactory.CreateDxgiSurfaceRenderTarget(surface, props);
        // ClearType needs an opaque backdrop; on a transparent surface it fringes, so grayscale AA.
        s.Target.TextAntialiasMode = Vortice.Direct2D1.TextAntialiasMode.Grayscale;
        s.Brush = s.Target.CreateSolidColorBrush(new Color4(1f, 1f, 1f, 1f));
        _layers[layer] = s;
        return s;
    }

    public void Dispose()
    {
        foreach (var s in _layers.Values) s.Dispose();
        _layers.Clear();
        foreach (var f in _formats.Values) f.Dispose();
        _formats.Clear();
        _dwriteFactory.Dispose();
        _d2dFactory.Dispose();
    }
}
