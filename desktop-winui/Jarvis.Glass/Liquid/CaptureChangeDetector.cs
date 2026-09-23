using System.Runtime.InteropServices;
using Vortice.Direct3D;
using Vortice.Direct3D11;
using Vortice.DXGI;

namespace Jarvis_Glass;

/// <summary>
/// Answers "did the desktop behind the window really change?" for capture frames whose dirty
/// rects fall entirely inside the window's own rect. Those are ambiguous: the window is excluded
/// from capture, but every Present of ours (and every XAML repaint) still makes DWM report the
/// window's rect dirty and deliver a frame -- with the same pixels behind it. Rendering on those
/// frames fed itself: render -> Present -> capture frame -> render, ~36 fps over a static desktop.
///
/// The host snapshots the window's crop of the capture whenever it renders; a check compares the
/// current capture against that snapshot on the GPU (ChangeDetect.hlsl discards identical texels
/// inside an occlusion query) and the sample count is read back a frame or two later without
/// stalling. UI thread only.
/// </summary>
internal sealed class CaptureChangeDetector : IDisposable
{
    private readonly ID3D11Device _device;
    private readonly ID3D11DeviceContext _context;
    private readonly ID3D11VertexShader _vs;
    private readonly ID3D11PixelShader _ps;
    private readonly ID3D11Buffer _constants;
    private readonly ID3D11Query _query;

    private ID3D11Texture2D? _reference;
    private ID3D11ShaderResourceView? _referenceSrv;
    private CropRect _referenceCrop;
    private bool _pending;

    public CaptureChangeDetector(ID3D11Device device, ID3D11DeviceContext context, string shaderDir)
    {
        _device = device;
        _context = context;
        var path = Path.Combine(shaderDir, "ChangeDetect.hlsl");
        _vs = ShaderPipeline.CreateVertexShader(device, path, "VSMain");
        _ps = ShaderPipeline.CreatePixelShader(device, path, "PSMain");
        _constants = ShaderPipeline.CreateConstantBuffer(device, 16);
        _query = device.CreateQuery(new QueryDescription(QueryType.Occlusion));
    }

    /// <summary>A check has been issued and its answer is not in yet.</summary>
    public bool Pending => _pending;

    /// <summary>Records what the render about to happen will see under the window. Call it before
    /// the draw: a frame copied in between then reads as a change (one spare render), never as
    /// "no change" (a missed one). Drops any in-flight check, which compared against the old
    /// snapshot.</summary>
    public void Snapshot(LiveCaptureSource capture, CropRect crop)
    {
        _pending = false;
        if (crop.Width <= 0 || crop.Height <= 0) return;
        if (_reference is null || _referenceCrop.Width != crop.Width || _referenceCrop.Height != crop.Height)
        {
            _referenceSrv?.Dispose();
            _reference?.Dispose();
            _reference = _device.CreateTexture2D(new Texture2DDescription
            {
                Width = (uint)crop.Width,
                Height = (uint)crop.Height,
                MipLevels = 1,
                ArraySize = 1,
                Format = Format.B8G8R8A8_UNorm,
                SampleDescription = new SampleDescription(1, 0),
                Usage = ResourceUsage.Default,
                BindFlags = BindFlags.ShaderResource,
            });
            _referenceSrv = _device.CreateShaderResourceView(_reference);
        }
        _referenceCrop = capture.CopyRegionTo(_reference, crop) ? crop : default;
    }

    /// <summary>Starts comparing the live capture's crop against the snapshot. Without a usable
    /// snapshot (none yet, or the window moved/resized since) returns true: treat as changed.</summary>
    public bool Begin(ID3D11ShaderResourceView captureSrv, CropRect crop)
    {
        if (_referenceSrv is null || crop != _referenceCrop || crop.Width <= 0 || crop.Height <= 0) return true;

        var mapped = _context.Map(_constants, 0, MapMode.WriteDiscard);
        try
        {
            Marshal.WriteInt32(mapped.DataPointer, 0, crop.X);
            Marshal.WriteInt32(mapped.DataPointer, 4, crop.Y);
        }
        finally
        {
            _context.Unmap(_constants, 0);
        }

        _context.OMSetRenderTargets(Array.Empty<ID3D11RenderTargetView>());
        _context.OMSetBlendState(null);
        _context.RSSetState(null);
        _context.RSSetViewport(0, 0, crop.Width, crop.Height);
        _context.IASetPrimitiveTopology(PrimitiveTopology.TriangleList);
        _context.IASetInputLayout(null);
        _context.VSSetShader(_vs);
        _context.PSSetShader(_ps);
        _context.PSSetConstantBuffer(0, _constants);
        _context.PSSetShaderResource(0, captureSrv);
        _context.PSSetShaderResource(1, _referenceSrv);
        _context.Begin(_query);
        _context.Draw(3, 0);
        _context.End(_query);
        _context.PSSetShaderResource(0, null!);
        _context.PSSetShaderResource(1, null!);
        _pending = true;
        return false;
    }

    /// <summary>Non-blocking. True once the pending check has an answer, in
    /// <paramref name="changed"/>.</summary>
    public unsafe bool TryTakeResult(out bool changed)
    {
        changed = false;
        if (!_pending) return false;
        ulong samples = 0;
        // S_FALSE (1) = not ready yet; DoNotFlush keeps this from forcing a submit.
        var hr = _context.GetData(_query, (nint)(&samples), sizeof(ulong), AsyncGetDataFlags.DoNotFlush);
        if (hr.Code != 0) return false;
        changed = samples != 0;
        _pending = false;
        return true;
    }

    public void Dispose()
    {
        _query.Dispose();
        _constants.Dispose();
        _ps.Dispose();
        _vs.Dispose();
        _referenceSrv?.Dispose();
        _reference?.Dispose();
    }
}

/// <summary>A rectangle in capture (display) pixels.</summary>
internal readonly record struct CropRect(int X, int Y, int Width, int Height)
{
    public bool Contains(Windows.Graphics.RectInt32 r) =>
        r.X >= X && r.Y >= Y && r.X + r.Width <= X + Width && r.Y + r.Height <= Y + Height;

    public bool Intersects(Windows.Graphics.RectInt32 r, int margin) =>
        r.X < X + Width + margin && r.X + r.Width > X - margin && r.Y < Y + Height + margin && r.Y + r.Height > Y - margin;
}
