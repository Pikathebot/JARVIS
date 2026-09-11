using System.Runtime.InteropServices;
using Vortice.Direct3D11;
using Vortice.DXGI;
using Windows.Graphics.DirectX.Direct3D11;

namespace Jarvis_GlassLab;

/// <summary>
/// The interop shim Win2D normally hides: wrapping a raw ID3D11Device/texture into the WinRT
/// IDirect3DDevice/IDirect3DSurface types Windows.Graphics.Capture speaks, and back out again.
/// desktop-winui/Jarvis.Glass never had to write this because CanvasDevice/CanvasBitmap do it for
/// free; going raw D3D11 here means doing it by hand, which is exactly the kind of thing the
/// GlassLab plan flagged as a real risk for this phase.
/// </summary>
internal static unsafe class Direct3D11Interop
{
    [DllImport("d3d11.dll", EntryPoint = "CreateDirect3D11DeviceFromDXGIDevice", PreserveSig = true)]
    private static extern int CreateDirect3D11DeviceFromDXGIDevice(nint dxgiDevice, out nint graphicsDevice);

    /// <summary>Wraps a Vortice IDXGIDevice into a WinRT IDirect3DDevice, so Direct3D11CaptureFramePool
    /// can be created against our own raw D3D11 device instead of a Win2D CanvasDevice.</summary>
    public static IDirect3DDevice CreateDirect3DDeviceFromDXGIDevice(IDXGIDevice dxgiDevice)
    {
        int hr = CreateDirect3D11DeviceFromDXGIDevice(dxgiDevice.NativePointer, out var inspectablePtr);
        Marshal.ThrowExceptionForHR(hr);
        try
        {
            return WinRT.MarshalInterface<IDirect3DDevice>.FromAbi(inspectablePtr);
        }
        finally
        {
            Marshal.Release(inspectablePtr);
        }
    }

    [ComImport]
    [Guid("A9B3D012-3DF2-4EE3-B8D1-8695F457D3C1")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IDirect3DDxgiInterfaceAccess
    {
        nint GetInterface(ref Guid iid);
    }

    /// <summary>Unwraps a captured frame's WinRT IDirect3DSurface back down to the underlying
    /// ID3D11Texture2D, so it can be copied/sampled through the raw D3D11 pipeline.</summary>
    public static ID3D11Texture2D GetTexture(IDirect3DSurface surface)
    {
        // A plain C# cast only works for interfaces CsWinRT already knows how to project; this
        // custom, non-WinRT COM interface needs a real QueryInterface via WinRT.CastExtensions.As,
        // not `(IDirect3DDxgiInterfaceAccess)(object)surface` (which throws InvalidCastException -
        // the bug that first blocked this phase's live-capture checkpoint).
        var access = WinRT.CastExtensions.As<IDirect3DDxgiInterfaceAccess>(surface);
        var iid = typeof(ID3D11Texture2D).GUID;
        var ptr = access.GetInterface(ref iid);
        return new ID3D11Texture2D(ptr);
    }
}
