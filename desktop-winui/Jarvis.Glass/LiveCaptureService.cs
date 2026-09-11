using Microsoft.Graphics.Canvas;
using Microsoft.UI.Windowing;
using Windows.Graphics.Capture;
using Windows.Graphics.DirectX;
using Windows.Graphics.DirectX.Direct3D11;
using Windows.Security.Authorization.AppCapabilityAccess;

namespace Jarvis_Glass;

/// <summary>
/// Captures the whole display the app window is on via Windows.Graphics.Capture and exposes the
/// latest frame as a shared CanvasBitmap, so Tier A glass can refract what is actually behind the
/// window (including an animated live-wallpaper layer no file on disk contains) instead of the
/// designed gradient WallpaperBitmapCache falls back to.
///
/// Shaped like WallpaperBitmapCache: one shared capture session, panels pull the latest frame
/// rather than each owning a capture of their own. Unlike the wallpaper cache, this is expensive
/// enough (continuous whole-screen capture) that it is reference-counted -- Start()/Stop() are
/// called once per GlassPanel that currently wants it, not once globally, and the underlying
/// capture session only actually runs while at least one caller is asking for it.
///
/// There is no supported "capture everything except this window" mode; a window-scoped
/// GraphicsCaptureItem would capture only the app's own window, which is useless here. Capturing
/// the whole display and letting the app's own window occlude itself in the captured frame is the
/// correct and only real option -- and harmless in practice, since the region under the window is
/// covered by opaque XAML content in the actual composited output regardless of what the capture
/// sees there.
/// </summary>
public static class LiveCaptureService
{
    /// <summary>Target capture cadence. Higher would cost more GPU/CPU for a cosmetic effect;
    /// lower starts to look laggy against a genuinely animated wallpaper.</summary>
    private const int TargetFps = 12;
    private static readonly TimeSpan MinFrameInterval = TimeSpan.FromMilliseconds(1000.0 / TargetFps);

    private static int _refCount;
    private static int _generation;
    private static CanvasDevice? _canvasDevice;
    private static IDirect3DDevice? _d3dDevice;
    private static GraphicsCaptureItem? _item;
    private static Direct3D11CaptureFramePool? _framePool;
    private static GraphicsCaptureSession? _session;
    private static DateTime _lastAcceptedFrame = DateTime.MinValue;
    private static readonly object _gate = new();

    /// <summary>The most recently captured frame, or null if capture is off/unavailable/not
    /// warmed up yet. Callers must not hold onto this across frames -- it is replaced (and the
    /// previous one disposed) as new frames arrive.</summary>
    public static CanvasBitmap? CurrentFrame { get; private set; }

    /// <summary>True once a capture session is actually running (not just requested) -- false
    /// covers "not started", "stopped", and "failed to start" alike, since all three mean the
    /// same thing to a caller: there is no live frame coming.</summary>
    public static bool IsActive { get; private set; }

    /// <summary>Raised whenever <see cref="CurrentFrame"/> changes, so panels can repaint.</summary>
    public static event Action? FrameUpdated;

    /// <summary>
    /// Requests the capture session run. Reference-counted: safe to call once per panel that wants
    /// live capture, in any order relative to <see cref="Stop"/>. Never throws -- a failure (no
    /// permission, API unavailable on this Windows version, no display found) leaves
    /// <see cref="IsActive"/> false and callers fall back to their next material source.
    /// </summary>
    public static void Start()
    {
        int generation;
        lock (_gate)
        {
            _refCount++;
            if (IsActive || _refCount != 1)
            {
                return;
            }
            generation = _generation;
        }

        // RequestAccessAsync (and the consent prompt it can show on first use) is async, but every
        // caller of Start() is a synchronous UI property-changed callback -- fire-and-forget here,
        // guarded by the generation counter so a Stop() that lands while consent is still pending
        // (or a Stop()+Start() cycle) doesn't race a session into existence after the caller gave up.
        _ = StartCaptureSessionAsync(generation);
    }

    private static async Task StartCaptureSessionAsync(int generation)
    {
        try
        {
            var status = await GraphicsCaptureAccess.RequestAccessAsync(GraphicsCaptureAccessKind.Programmatic);
            if (status != AppCapabilityAccessStatus.Allowed)
            {
                return;
            }

            lock (_gate)
            {
                if (generation != _generation || _refCount == 0)
                {
                    // Stopped (or stopped-and-restarted) while the consent prompt/check was in flight.
                    return;
                }

                // Must happen before the capture session actually starts, not after -- otherwise
                // the first frame(s) would still see our own opaque panel and reintroduce exactly
                // the feedback loop this exists to prevent. See WindowCaptureExclusion's own doc
                // comment for why this is the actual fix rather than a cropping workaround.
                WindowCaptureExclusion.SetExcluded(true);
                StartCaptureSession();
                IsActive = true;
            }
        }
        catch
        {
            // No GraphicsCapturePermissionStatus / API absent on this OS build / no display --
            // any of these mean "no live capture", not "crash the glass material system".
            lock (_gate)
            {
                TeardownSession();
                IsActive = false;
                WindowCaptureExclusion.SetExcluded(false);
            }
        }
    }

    /// <summary>Releases one reference. The capture session actually stops only once every caller
    /// that asked for it has also let go.</summary>
    public static void Stop()
    {
        lock (_gate)
        {
            if (_refCount == 0) return;
            _refCount--;
            if (_refCount > 0) return;

            _generation++;
            TeardownSession();
            IsActive = false;
            WindowCaptureExclusion.SetExcluded(false);
            var stale = CurrentFrame;
            CurrentFrame = null;
            stale?.Dispose();
        }
    }

    private static void StartCaptureSession()
    {
        // Win2D's CanvasDevice implements IDirect3DDevice directly (see Win2D's own
        // Windows.Graphics.Capture interop sample) -- sharing this one device between the capture
        // frame pool and CanvasBitmap.CreateFromDirect3D11Surface below is what keeps the
        // surface->bitmap wrap on the same adapter/device rather than forcing a cross-device copy.
        _canvasDevice ??= new CanvasDevice();
        _d3dDevice = (IDirect3DDevice)_canvasDevice;

        var displayArea = DisplayArea.Primary
            ?? throw new InvalidOperationException("No primary display area available for capture.");
        var displayId = new Windows.Graphics.DisplayId { Value = displayArea.DisplayId.Value };

        _item = GraphicsCaptureItem.TryCreateFromDisplayId(displayId)
            ?? throw new InvalidOperationException("Windows.Graphics.Capture is unavailable for this display.");

        _framePool = Direct3D11CaptureFramePool.CreateFreeThreaded(
            _d3dDevice,
            DirectXPixelFormat.B8G8R8A8UIntNormalized,
            2,
            _item.Size);

        _framePool.FrameArrived += OnFrameArrived;

        _session = _framePool.CreateCaptureSession(_item);
        // We only want the desktop behind our own window, not a yellow capture border drawn by
        // the OS over the top of it. Both properties require 19041+ (the app's own min version is
        // 17763); the try/catch is the real runtime guard on older Windows, matching this
        // project's requirement to degrade gracefully rather than crash -- so the platform-compat
        // analyzer's static warning is suppressed rather than fixed with an OS-version branch.
#pragma warning disable CA1416
        try { _session.IsBorderRequired = false; } catch { /* older WindowsAppSDK: property absent */ }
        try { _session.IsCursorCaptureEnabled = false; } catch { /* same */ }
#pragma warning restore CA1416
        _session.StartCapture();
    }

    private static void OnFrameArrived(Direct3D11CaptureFramePool sender, object args)
    {
        using var frame = sender.TryGetNextFrame();
        if (frame is null) return;

        var now = DateTime.UtcNow;
        if (now - _lastAcceptedFrame < MinFrameInterval)
        {
            // Throttled: drop this frame without doing the (comparatively expensive) surface wrap.
            return;
        }
        _lastAcceptedFrame = now;

        try
        {
            if (_canvasDevice is null) return;
            var bitmap = CanvasBitmap.CreateFromDirect3D11Surface(_canvasDevice, frame.Surface);

            lock (_gate)
            {
                if (!IsActive) { bitmap.Dispose(); return; }
                var stale = CurrentFrame;
                CurrentFrame = bitmap;
                stale?.Dispose();
            }

            FrameUpdated?.Invoke();
        }
        catch
        {
            // A single bad frame (device lost, surface interop failure) should not tear down the
            // whole session -- the next frame gets another chance.
        }
    }

    private static void TeardownSession()
    {
        try { _session?.Dispose(); } catch { /* best-effort */ }
        _session = null;

        if (_framePool is not null)
        {
            try { _framePool.FrameArrived -= OnFrameArrived; } catch { /* best-effort */ }
            try { _framePool.Dispose(); } catch { /* best-effort */ }
            _framePool = null;
        }

        _item = null;
        _d3dDevice = null;
    }
}
