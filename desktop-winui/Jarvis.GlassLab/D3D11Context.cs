using Vortice.Direct3D;
using Vortice.Direct3D11;
using Vortice.DXGI;

namespace Jarvis_GlassLab;

/// <summary>
/// Owns the D3D11 device/context and a DXGI swapchain created for DirectComposition (not for an
/// HWND directly - CreateSwapChainForComposition is what lets the swapchain's content be handed
/// to an IDCompositionVisual instead of being bound to a window's own redirection surface, which
/// doesn't exist here since the window was created with WS_EX_NOREDIRECTIONBITMAP).
/// </summary>
internal sealed class D3D11Context : IDisposable
{
    public ID3D11Device Device { get; }
    public ID3D11DeviceContext ImmediateContext { get; }
    public IDXGISwapChain1 SwapChain { get; }
    public IDXGIDevice DxgiDevice { get; }

    private ID3D11Texture2D? _backBuffer;
    private ID3D11RenderTargetView? _renderTargetView;

    public ID3D11RenderTargetView RenderTargetView => _renderTargetView!;
    public int Width { get; private set; }
    public int Height { get; private set; }

    /// <summary>Debug: copies the back buffer to a staging texture and writes it as raw
    /// top-down BGRA (width*height*4 bytes) -- the only way to see what this window renders once
    /// it is excluded from capture, since screenshots then can't see it. Call after Draw, before
    /// Present.</summary>
    public void DumpBackBuffer(string path)
    {
        using var staging = Device.CreateTexture2D(new Texture2DDescription
        {
            Width = (uint)Width,
            Height = (uint)Height,
            MipLevels = 1,
            ArraySize = 1,
            Format = Format.B8G8R8A8_UNorm,
            SampleDescription = new SampleDescription(1, 0),
            Usage = ResourceUsage.Staging,
            CPUAccessFlags = CpuAccessFlags.Read,
        });
        ImmediateContext.CopyResource(staging, _backBuffer!);
        var mapped = ImmediateContext.Map(staging, 0, MapMode.Read);
        try
        {
            var bytes = new byte[Width * Height * 4];
            unsafe
            {
                for (var y = 0; y < Height; y++)
                {
                    new ReadOnlySpan<byte>((byte*)mapped.DataPointer + y * mapped.RowPitch, Width * 4)
                        .CopyTo(bytes.AsSpan(y * Width * 4));
                }
            }
            File.WriteAllBytes(path, bytes);
        }
        finally
        {
            ImmediateContext.Unmap(staging, 0);
        }
    }

    public D3D11Context(int width, int height)
    {
        Width = width;
        Height = height;
        var featureLevels = new[] { FeatureLevel.Level_11_0 };

        // BgraSupport is required for DirectComposition/DXGI interop - the swapchain format below
        // (B8G8R8A8_UNorm) has to match a device created with this flag or CreateSwapChainForComposition
        // fails.
        D3D11.D3D11CreateDevice(
            null,
            DriverType.Hardware,
            DeviceCreationFlags.BgraSupport,
            featureLevels,
            out ID3D11Device? device,
            out ID3D11DeviceContext? context).CheckError();

        Device = device!;
        ImmediateContext = context!;
        DxgiDevice = Device.QueryInterface<IDXGIDevice>();

        using var adapter = DxgiDevice.GetAdapter();
        using var factory = adapter.GetParent<IDXGIFactory2>();

        var swapChainDesc = new SwapChainDescription1
        {
            Width = (uint)width,
            Height = (uint)height,
            Format = Format.B8G8R8A8_UNorm,
            Stereo = false,
            SampleDescription = new SampleDescription(1, 0),
            BufferUsage = Usage.RenderTargetOutput,
            BufferCount = 2,
            Scaling = Scaling.Stretch,
            SwapEffect = SwapEffect.FlipSequential,
            AlphaMode = Vortice.DXGI.AlphaMode.Premultiplied,
        };

        // CreateSwapChainForComposition (not CreateSwapChainForHwnd) - this swapchain has no
        // window of its own; DirectComposition attaches it to a visual instead (see
        // CompositionContext).
        SwapChain = factory.CreateSwapChainForComposition(Device, swapChainDesc);

        CreateRenderTargetView();
    }

    private void CreateRenderTargetView()
    {
        _renderTargetView?.Dispose();
        _backBuffer?.Dispose();

        _backBuffer = SwapChain.GetBuffer<ID3D11Texture2D>(0);
        _renderTargetView = Device.CreateRenderTargetView(_backBuffer);
    }

    /// <summary>
    /// Clears the back buffer to a premultiplied-alpha color and presents once. Premultiplied:
    /// the RGB channels must already be scaled by alpha (e.g. 50%-alpha magenta is (0.5,0,0.5,0.5),
    /// not (1,0,1,0.5)) - DWM composites this swapchain with DXGI_ALPHA_MODE_PREMULTIPLIED, so a
    /// naive straight-alpha clear color would look wrong (too bright/washed out) once composited
    /// over the desktop.
    /// </summary>
    public void ClearAndPresent(float r, float g, float b, float a)
    {
        ImmediateContext.ClearRenderTargetView(_renderTargetView, new Vortice.Mathematics.Color4(r, g, b, a));
        SwapChain.Present(1, PresentFlags.None);
    }

    public void Present()
    {
        SwapChain.Present(1, PresentFlags.None);
    }

    /// <summary>
    /// Resizes the swapchain's buffers to match the window's new size. All views bound to the old
    /// back buffer must be released first - the render target view and the backing texture both
    /// hold a reference that would otherwise make ResizeBuffers fail (DXGI_ERROR_INVALID_CALL).
    /// No-ops if the size hasn't actually changed, since resize handlers can fire redundantly
    /// (e.g. WM_WINDOWPOSCHANGED on a pure move).
    /// </summary>
    public void ResizeBuffers(int width, int height)
    {
        if (width == Width && height == Height || width <= 0 || height <= 0)
        {
            return;
        }

        _renderTargetView?.Dispose();
        _renderTargetView = null;
        _backBuffer?.Dispose();
        _backBuffer = null;

        SwapChain.ResizeBuffers(2, (uint)width, (uint)height, Format.B8G8R8A8_UNorm, SwapChainFlags.None);

        Width = width;
        Height = height;
        CreateRenderTargetView();
    }

    public void Dispose()
    {
        _renderTargetView?.Dispose();
        _backBuffer?.Dispose();
        SwapChain?.Dispose();
        DxgiDevice?.Dispose();
        ImmediateContext?.Dispose();
        Device?.Dispose();
    }
}
