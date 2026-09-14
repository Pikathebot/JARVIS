using Vortice.Direct3D11;
using Vortice.DirectComposition;
using Vortice.DXGI;

namespace Jarvis_Glass;

/// <summary>
/// Binds a DXGI swapchain (created for composition, not for an HWND) into the desktop's visual
/// tree via DirectComposition: device -> target (bound to our HWND) -> visual (content = the
/// swapchain) -> Commit(). This is what actually makes the swapchain's pixels show up on screen,
/// composited by DWM against whatever is beneath our window in Z-order.
/// </summary>
internal sealed class CompositionContext : IDisposable
{
    private readonly IDCompositionDevice _device;
    private readonly IDCompositionTarget _target;
    private readonly IDCompositionVisual _visual;
    private readonly IDXGISwapChain1 _swapChain;

    public CompositionContext(IDXGIDevice dxgiDevice, nint hwnd, IDXGISwapChain1 swapChain)
    {
        _swapChain = swapChain;
        _device = DComp.DCompositionCreateDevice<IDCompositionDevice>(dxgiDevice);

        // "topmost" here means the target sits at the top of *this window's own* composition tree,
        // not the OS Z-order (that's handled by WS_EX_TOPMOST on the HWND itself).
        _device.CreateTargetForHwnd(hwnd, true, out _target).CheckError();

        _visual = _device.CreateVisual();
        _visual.SetContent(swapChain);
        _target.SetRoot(_visual);
        _device.Commit();
    }

    /// <summary>
    /// Detaches the visual's content before a swapchain ResizeBuffers call and reattaches it
    /// after. The swapchain COM object reference doesn't actually change across a resize, so this
    /// is defensive rather than strictly required in every driver/OS combination - but
    /// ResizeBuffers while a swapchain is still bound as a composition visual's content is a
    /// documented source of resize failures on some configurations, and clearing/reattaching is
    /// the standard workaround.
    /// </summary>
    public void AroundResize(Action resize)
    {
        _visual.SetContent(null);
        _device.Commit();

        resize();

        _visual.SetContent(_swapChain);
        _device.Commit();
    }

    public void Dispose()
    {
        _visual?.Dispose();
        _target?.Dispose();
        _device?.Dispose();
    }
}
