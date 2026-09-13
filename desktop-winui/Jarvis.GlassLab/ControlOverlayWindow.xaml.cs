using System.Runtime.InteropServices;
using Microsoft.UI;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Media;
using Windows.Graphics;
using Windows.Win32;
using Windows.Win32.Foundation;
using Windows.Win32.UI.WindowsAndMessaging;

namespace Jarvis_GlassLab;

/// <summary>
/// Phase 7: a normal WinUI3 window hosting real controls, stacked above the glass HWND rather
/// than merged into its composition tree (see the GlassLab plan doc for why - merging a raw
/// DirectComposition visual into WinUI3's own XAML compositor is undocumented/fragile territory,
/// while two owner-linked, DWM-stacked windows is a well-understood pattern). Made see-through
/// with a <see cref="TransparentBackdrop"/> (a CompositionBrushBackdrop whose brush is a fully
/// transparent color) rather than WS_EX_LAYERED color-keying, which is unsupported on a WinUI3
/// XAML island. Found the hard way (2026-09-13): a Transparent XAML root Grid alone is NOT
/// enough -- the window's own surface then paints opaque black, which is exactly the "glass body
/// is solid black" symptom that was chased through the whole D3D pipeline before an A/B with
/// this overlay hidden showed the glass underneath rendering the backdrop perfectly. Click-through
/// everywhere except the controls' actual screen bounds, via a subclassed WndProc intercepting
/// WM_NCHITTEST.
/// </summary>
public sealed partial class ControlOverlayWindow : Window
{
    private WNDPROC? _subclassProc;
    private WNDPROC _originalWndProc = null!;
    private HWND _hwnd;
    private AppWindow _appWindow = null!;
    private readonly GlassRenderer _renderer;

    internal HWND Handle => _hwnd;

    /// <summary>Hit-tested against WM_NCHITTEST -- every interactive control needs an entry here
    /// or clicks fall through to the desktop underneath, per the "maintained hit-region list" the
    /// Phase 7 plan called for once more than one control existed.</summary>
    private FrameworkElement[] HitRegions => new FrameworkElement[] { TestButton, LipProfileToggle, BezelWidthSlider, CornerRadiusSlider, DemoToggle, DemoSlider }
        .Concat(_tuningSliders).ToArray();

    private readonly List<FrameworkElement> _tuningSliders = new();

    /// <summary>One row per tunable of GlassToggle.Material: label, min, max, getter, setter.</summary>
    private static readonly (string Label, float Min, float Max, Func<float> Get, Action<float> Set)[] ToggleTunables =
    {
        ("Force lift (debug)", 0f, 1f, () => GlassToggle.Material.ForceLift, v => GlassToggle.Material.ForceLift = v),
        ("Lift scale", 1f, 1.8f, () => GlassToggle.Material.LiftScale, v => GlassToggle.Material.LiftScale = v),
        ("Lift tint", 0f, 1f, () => GlassToggle.Material.LiftTint, v => GlassToggle.Material.LiftTint = v),
        ("Lift refraction", 0f, 40f, () => GlassToggle.Material.LiftRefraction, v => GlassToggle.Material.LiftRefraction = v),
        ("Lift bezel frac", 0.1f, 1f, () => GlassToggle.Material.LiftBezelFraction, v => GlassToggle.Material.LiftBezelFraction = v),
        ("Lift chromatic", 0f, 0.5f, () => GlassToggle.Material.LiftChromatic, v => GlassToggle.Material.LiftChromatic = v),
        ("Lift specular", 0f, 2.5f, () => GlassToggle.Material.LiftSpecular, v => GlassToggle.Material.LiftSpecular = v),
        ("Lift blur px", 0f, 8f, () => GlassToggle.Material.LiftBlur, v => GlassToggle.Material.LiftBlur = v),
        ("Stretch", 0f, 0.2f, () => GlassToggle.Material.ThumbStretch, v => GlassToggle.Material.ThumbStretch = v),
        ("Rest tint", 0f, 1f, () => GlassToggle.Material.RestTint, v => GlassToggle.Material.RestTint = v),
        ("Rest shadow", 0f, 0.8f, () => GlassToggle.Material.RestShadow, v => GlassToggle.Material.RestShadow = v),
        ("Shadow radius", 0f, 20f, () => GlassToggle.Material.RestShadowRadius, v => GlassToggle.Material.RestShadowRadius = v),
        ("Track specular", 0f, 2f, () => GlassToggle.Material.TrackSpecular, v => GlassToggle.Material.TrackSpecular = v),
        ("Slider thumb aspect", 0.6f, 2.2f, () => GlassSlider.Material.ThumbAspect, v => GlassSlider.Material.ThumbAspect = v),
    };

    private void BuildTuningPanel()
    {
        foreach (var (label, min, max, get, set) in ToggleTunables)
        {
            var row = new Microsoft.UI.Xaml.Controls.Grid { ColumnSpacing = 8 };
            row.ColumnDefinitions.Add(new Microsoft.UI.Xaml.Controls.ColumnDefinition { Width = new GridLength(110) });
            row.ColumnDefinitions.Add(new Microsoft.UI.Xaml.Controls.ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            var text = new Microsoft.UI.Xaml.Controls.TextBlock
            {
                Text = label, FontSize = 11, VerticalAlignment = VerticalAlignment.Center,
                Foreground = new Microsoft.UI.Xaml.Media.SolidColorBrush(Microsoft.UI.Colors.White),
            };
            var slider = new Microsoft.UI.Xaml.Controls.Slider
            {
                Minimum = min, Maximum = max, StepFrequency = (max - min) / 100.0, Value = get(),
                MinWidth = 120, Margin = new Thickness(0, -6, 0, -6),
            };
            var setter = set;
            slider.ValueChanged += (_, e) => { setter((float)e.NewValue); GlassToggle.MaterialChanged(); };
            Microsoft.UI.Xaml.Controls.Grid.SetColumn(slider, 1);
            row.Children.Add(text);
            row.Children.Add(slider);
            TuningPanel.Children.Add(row);
            _tuningSliders.Add(slider);
        }
    }

    internal ControlOverlayWindow(nint ownerHwndValue, GlassRenderer renderer)
    {
        _renderer = renderer;
        InitializeComponent();
        SystemBackdrop = new TransparentBackdrop();

        BuildTuningPanel();
        DemoSlider.Value = 0.35;
        Card.LayoutUpdated += (_, _) => PublishCardShape();
        Card.Loaded += (_, _) => PublishCardShape();
        BezelWidthSlider.Value = _renderer.BezelWidth;
        CornerRadiusSlider.Value = _renderer.CornerRadius;
        LipProfileToggle.IsOn = _renderer.Profile == GlassBezelProfile.Lip;

        var hwndValue = WinRT.Interop.WindowNative.GetWindowHandle(this);
        _hwnd = (HWND)hwndValue;

        var windowId = Win32Interop.GetWindowIdFromWindow(hwndValue);
        _appWindow = AppWindow.GetFromWindowId(windowId);

        // Borderless, no title bar, no resize/maximize/minimize chrome, always on top, hidden
        // from Alt+Tab/taskbar - this window exists purely to host controls above the glass
        // layer, not to be a normal app window in its own right.
        if (_appWindow.Presenter is OverlappedPresenter presenter)
        {
            presenter.SetBorderAndTitleBar(false, false);
            presenter.IsResizable = false;
            presenter.IsMaximizable = false;
            presenter.IsMinimizable = false;
            presenter.IsAlwaysOnTop = true;
        }
        _appWindow.IsShownInSwitchers = false;

        // Owns this window to the glass HWND so they minimize/close together and stack correctly,
        // without making this a child window (which would clip it to the parent's bounds).
        PInvoke.SetWindowLongPtr(_hwnd, WINDOW_LONG_PTR_INDEX.GWLP_HWNDPARENT, ownerHwndValue);

        // Same DWM trick GlassWindow uses for its raw HWND: enabling blur-behind with no region
        // makes DWM honour the window's per-pixel alpha, so the areas the XAML tree leaves
        // transparent (see TransparentBackdrop) actually show the glass window beneath instead of
        // being filled opaque black. Without this the transparent backdrop brush alone does
        // nothing visible.
        var blurBehind = new Windows.Win32.Graphics.Dwm.DWM_BLURBEHIND
        {
            dwFlags = PInvoke.DWM_BB_ENABLE,
            fEnable = true,
            hRgnBlur = Windows.Win32.Graphics.Gdi.HRGN.Null,
        };
        PInvoke.DwmEnableBlurBehindWindow(_hwnd, in blurBehind);

        SubclassWndProc();
    }

    /// <summary>Keeps this window pixel-locked to the glass window's screen rect. Called from
    /// App whenever the glass window's position/size changes.</summary>
    public void SyncBounds(int x, int y, int width, int height)
    {
        _appWindow.MoveAndResize(new RectInt32(x, y, width, height));
    }

    /// <summary>Hidden alongside GlassWindow during each periodic snapshot capture (see
    /// App.RefreshSnapshotAsync) -- this overlay's own control panel sits at the same screen
    /// position and would otherwise contaminate the "clean" snapshot with its own dark
    /// background.</summary>
    public void SetVisible(bool visible)
    {
        if (visible) _appWindow.Show(false);
        else _appWindow.Hide();
    }

    private void TestButton_Click(object sender, RoutedEventArgs e)
    {
        TestButton.Content = TestButton.Content is "Clicked!" ? "GlassLab test button" : "Clicked!";
        var logPath = LiveCaptureSource.DiagnosticLogPath;
        if (logPath is not null)
        {
            try { File.AppendAllText(logPath, $"[{DateTimeOffset.Now:O}] ControlOverlayWindow: test button clicked\n"); } catch { }
        }
    }

    /// <summary>kube.io's own switch/slider pills read at roughly a 32px-tall track: corner
    /// radius = half the track height (a true pill), bezel band a bit over a third of that radius
    /// so the concave inner lobe has room to read as concave rather than degenerating into a
    /// point. The panel/squircle numbers (48/36) were tuned for this window's own ~900x600 body
    /// and are proportionally far too heavy for a control-scale bezel.</summary>
    private const float LipCornerRadius = 16f;
    private const float LipBezelWidth = 7f;
    private const float SquircleCornerRadius = 48f;
    private const float SquircleBezelWidth = 36f;

    /// <summary>Flips GlassRenderer.Profile live and swaps in the profile-appropriate bezel
    /// defaults -- no explicit redraw call needed, since the window's own render timer
    /// (App.StartCaptureAsync, ~12fps once live capture is flowing) picks up the new values on
    /// its next tick.</summary>
    private void LipProfileToggle_Toggled(object sender, RoutedEventArgs e)
    {
        _renderer.Profile = LipProfileToggle.IsOn ? GlassBezelProfile.Lip : GlassBezelProfile.Squircle;
        _renderer.CornerRadius = LipProfileToggle.IsOn ? LipCornerRadius : SquircleCornerRadius;
        _renderer.BezelWidth = LipProfileToggle.IsOn ? LipBezelWidth : SquircleBezelWidth;

        // Reflects the auto-picked defaults back into the sliders rather than leaving them stale
        // -- without this the sliders would silently disagree with what the renderer is actually
        // using until the user drags one themselves.
        CornerRadiusSlider.Value = _renderer.CornerRadius;
        BuildTuningPanel();
        DemoSlider.Value = 0.35;
        Card.LayoutUpdated += (_, _) => PublishCardShape();
        Card.Loaded += (_, _) => PublishCardShape();
        BezelWidthSlider.Value = _renderer.BezelWidth;
    }

    /// <summary>The control card as a glass slab: frosted, dark-tinted, on layer 1 so the
    /// controls (layers 2+) refract it rather than being covered by it.</summary>
    private void PublishCardShape()
    {
        if (Card.XamlRoot is null || Card.ActualWidth <= 0) return;
        var scale = (float)Card.XamlRoot.RasterizationScale;
        var b = Card.TransformToVisual(null).TransformBounds(new Windows.Foundation.Rect(0, 0, Card.ActualWidth, Card.ActualHeight));
        var center = new System.Numerics.Vector2((float)(b.X + b.Width / 2), (float)(b.Y + b.Height / 2)) * scale;
        var half = new System.Numerics.Vector2((float)b.Width / 2, (float)b.Height / 2) * scale;
        GlassShapeRegistry.Publish(Card, GlassShape.Create(
            center, half, cornerRadius: 14f * scale, bezelWidth: 10f * scale, GlassBezelProfile.Squircle,
            refractionScale: 6f * scale, specularIntensity: 0.5f, layer: 1,
            tintColor: new System.Numerics.Vector3(0.08f, 0.09f, 0.13f), tintAmount: 0.45f, blurRadius: 10f * scale));
    }

    private void DemoSlider_ValueChanged(object sender, RoutedEventArgs e)
    {
        DemoSliderLabel.Text = $"{DemoSlider.Value * 100:0}%";
    }

    private void DemoToggle_Toggled(object sender, RoutedEventArgs e)
    {
        DemoToggleLabel.Text = DemoToggle.IsOn ? "Glass toggle: on" : "Glass toggle: off";
    }

    private void BezelWidthSlider_ValueChanged(object sender, Microsoft.UI.Xaml.Controls.Primitives.RangeBaseValueChangedEventArgs e)
    {
        _renderer.BezelWidth = (float)e.NewValue;
    }

    private void CornerRadiusSlider_ValueChanged(object sender, Microsoft.UI.Xaml.Controls.Primitives.RangeBaseValueChangedEventArgs e)
    {
        _renderer.CornerRadius = (float)e.NewValue;
    }

    private void SubclassWndProc()
    {
        _subclassProc = WndProc;
        var newProcPtr = Marshal.GetFunctionPointerForDelegate(_subclassProc);
        var originalPtr = PInvoke.SetWindowLongPtr(_hwnd, WINDOW_LONG_PTR_INDEX.GWLP_WNDPROC, newProcPtr);
        _originalWndProc = Marshal.GetDelegateForFunctionPointer<WNDPROC>(originalPtr);
    }

    private LRESULT WndProc(HWND hwnd, uint msg, WPARAM wParam, LPARAM lParam)
    {
        if (msg == PInvoke.WM_NCHITTEST)
        {
            var screenX = unchecked((short)(lParam.Value & 0xFFFF));
            var screenY = unchecked((short)((lParam.Value >> 16) & 0xFFFF));
            var over = IsPointOverControl(screenX, screenY);
            return new LRESULT(over ? HTCLIENT : HTTRANSPARENT);
        }

        return PInvoke.CallWindowProc(_originalWndProc, hwnd, msg, wParam, lParam);
    }

    /// <summary>
    /// Translates each region in <see cref="HitRegions"/> from XAML-space bounds to screen
    /// pixels and tests the point against all of them -- the "maintained hit-region list" the
    /// Phase 7 plan called for once more than one control existed (the profile toggle and two
    /// sliders, added alongside the original test button).
    /// </summary>
    private bool IsPointOverControl(int screenX, int screenY)
    {
        foreach (var region in HitRegions)
        {
            try
            {
                var scale = region.XamlRoot?.RasterizationScale ?? 1.0;
                var transform = region.TransformToVisual(RootGrid);
                var bounds = transform.TransformBounds(new Windows.Foundation.Rect(0, 0, region.ActualWidth, region.ActualHeight));

                var pos = _appWindow.Position;
                var left = pos.X + (int)(bounds.X * scale);
                var top = pos.Y + (int)(bounds.Y * scale);
                var right = left + (int)(bounds.Width * scale);
                var bottom = top + (int)(bounds.Height * scale);

                if (screenX >= left && screenX < right && screenY >= top && screenY < bottom)
                {
                    return true;
                }
            }
            catch
            {
                // XamlRoot not ready yet (e.g. very first hit-test before layout has run) - skip
                // this region rather than accidentally swallowing a click meant for the desktop.
            }
        }

        return false;
    }

    private const int HTCLIENT = 1;
    private const int HTTRANSPARENT = -1;
}

/// <summary>The one way a WinUI3 top-level window's unpainted area actually becomes transparent
/// to DWM: hand the XAML compositor a backdrop brush with zero alpha. Same mechanism as WinUIEx's
/// TransparentTintBackdrop, inlined to avoid the dependency.</summary>
internal sealed partial class TransparentBackdrop : SystemBackdrop
{
    private Windows.UI.Composition.Compositor? _systemCompositor;
    private static object? _dispatcherQueueController;

    /// <summary>A system Compositor needs a Windows.System.DispatcherQueue on its thread; the
    /// WinUI thread only has the Microsoft.UI.Dispatching one, so create the system one here
    /// (DQTYPE_THREAD_CURRENT, DQTAT_COM_NONE) -- the same one-liner every WinUI3 sample that
    /// touches the system compositor carries.</summary>
    private static void EnsureSystemDispatcherQueue()
    {
        if (_dispatcherQueueController is not null || Windows.System.DispatcherQueue.GetForCurrentThread() is not null)
        {
            return;
        }

        var options = new DispatcherQueueOptions
        {
            dwSize = Marshal.SizeOf<DispatcherQueueOptions>(),
            threadType = 2,   // DQTYPE_THREAD_CURRENT
            apartmentType = 0 // DQTAT_COM_NONE
        };
        var hr = CreateDispatcherQueueController(options, out var controller);
        Marshal.ThrowExceptionForHR(hr);
        _dispatcherQueueController = controller;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct DispatcherQueueOptions
    {
        public int dwSize;
        public int threadType;
        public int apartmentType;
    }

    [DllImport("CoreMessaging.dll")]
    private static extern int CreateDispatcherQueueController(DispatcherQueueOptions options, [MarshalAs(UnmanagedType.IUnknown)] out object dispatcherQueueController);

    protected override void OnTargetConnected(Microsoft.UI.Composition.ICompositionSupportsSystemBackdrop connectedTarget, XamlRoot xamlRoot)
    {
        base.OnTargetConnected(connectedTarget, xamlRoot);
        // This WinAppSDK (2.4) types the target's SystemBackdrop slot as a *system*
        // (Windows.UI.Composition) brush, not a Microsoft.UI.Composition one -- the XAML
        // compositor's brushes are a different runtime class and a WinRT cast throws. So the brush
        // comes from a system Compositor created on this (DispatcherQueue-owning) XAML thread.
        if (_systemCompositor is null)
        {
            EnsureSystemDispatcherQueue();
            _systemCompositor = new Windows.UI.Composition.Compositor();
        }
        connectedTarget.SystemBackdrop = _systemCompositor.CreateColorBrush(Windows.UI.Color.FromArgb(0, 0, 0, 0));
    }

    protected override void OnTargetDisconnected(Microsoft.UI.Composition.ICompositionSupportsSystemBackdrop disconnectedTarget)
    {
        disconnectedTarget.SystemBackdrop = null;
        base.OnTargetDisconnected(disconnectedTarget);
    }
}
