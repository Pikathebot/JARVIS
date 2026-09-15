using System.Runtime.InteropServices;
using Jarvis.Core.Api;
using Jarvis.Core.Models;
using Jarvis_App.ViewModels;
using Microsoft.UI;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Input;

namespace Jarvis_App;

/// <summary>
/// Port of desktop-app/src/app/hud/page.tsx + the window-management half of
/// desktop-app/src-tauri/src/main.rs. 460x108-ish, borderless, always-on-top, no taskbar entry,
/// drag-anywhere, positioned bottom-right of the *current* monitor's work area (DisplayArea
/// already excludes the taskbar, replacing the Rust "margin * 3" hack). Runs its own chat session
/// against the non-streaming POST /chat and speaks every reply; never speaks awareness
/// observations — the main window owns those, matching the TS HUD_SESSION_ID convention.
/// </summary>
public sealed partial class HudWindow : Window
{
    private const string HudSessionId = "jarvis-hud";
    private const double MarginLogicalPx = 24.0;

    private readonly JarvisApiClient _api;
    private readonly AppWindow _appWindow;
    private readonly PeriodicTimer _telemetryTimer = new(TimeSpan.FromSeconds(4));
    private CancellationTokenSource? _telemetryCts;
    public VoiceViewModel VoiceViewModel { get; }

    /// <summary>The liquid-glass renderer behind this window (see GlassHost). Created before the
    /// window is first shown so the backdrop HWND exists and is owner-linked from the start.</summary>
    public Jarvis_Glass.GlassHost Glass { get; }

    public HudWindow(JarvisApiClient api)
    {
        InitializeComponent();
        _api = api;
        VoiceViewModel = new VoiceViewModel(api, DispatcherQueue, HudSessionId);
        VoiceViewModel.PropertyChanged += (_, args) =>
        {
            if (args.PropertyName is nameof(ViewModels.VoiceViewModel.State) or nameof(ViewModels.VoiceViewModel.Level))
            {
                DispatcherQueue.TryEnqueue(UpdateVoiceVisuals);
            }
        };
        VoiceViewModel.CommandReceived += query => DispatcherQueue.TryEnqueue(async () => await HandleCommandAsync(query));

        var hwnd = WinRT.Interop.WindowNative.GetWindowHandle(this);
        var windowId = Win32Interop.GetWindowIdFromWindow(hwnd);
        _appWindow = AppWindow.GetFromWindowId(windowId);

        _appWindow.Resize(new Windows.Graphics.SizeInt32(460, 108));
        _appWindow.IsShownInSwitchers = false;

        if (_appWindow.Presenter is OverlappedPresenter presenter)
        {
            presenter.SetBorderAndTitleBar(false, false);
            presenter.IsAlwaysOnTop = true;
            presenter.IsResizable = false;
            presenter.IsMaximizable = false;
            presenter.IsMinimizable = false;
        }

        _appWindow.Hide();

        // Liquid glass under the card's XAML tree.
        Glass = new Jarvis_Glass.GlassHost(this);
        Jarvis_Glass.GlassWindowChrome.HideBorder(this);
    }

    public void ToggleVisible()
    {
        if (_appWindow.IsVisible)
        {
            _appWindow.Hide();
            StopTelemetry();
        }
        else
        {
            PositionBottomRight();
            _appWindow.Show();
            StartTelemetry();
        }
    }

    private void PositionBottomRight()
    {
        var hwnd = WinRT.Interop.WindowNative.GetWindowHandle(this);
        var windowId = Win32Interop.GetWindowIdFromWindow(hwnd);
        var displayArea = DisplayArea.GetFromWindowId(windowId, DisplayAreaFallback.Nearest);
        var scale = GetDpiScale(hwnd);
        var margin = (int)(MarginLogicalPx * scale);

        var workArea = displayArea.WorkArea; // already excludes the taskbar
        var size = _appWindow.Size;
        var x = workArea.X + workArea.Width - size.Width - margin;
        var y = workArea.Y + workArea.Height - size.Height - margin;
        _appWindow.Move(new Windows.Graphics.PointInt32(x, y));
    }

    private static double GetDpiScale(nint hwnd)
    {
        var dpi = GetDpiForWindow(hwnd);
        return dpi / 96.0;
    }

    [DllImport("user32.dll")]
    private static extern uint GetDpiForWindow(nint hwnd);

    // --- Drag anywhere on the card, since there is no title bar (data-tauri-drag-region equivalent) ---

    [DllImport("user32.dll")]
    private static extern bool ReleaseCapture();

    [DllImport("user32.dll")]
    private static extern nint SendMessage(nint hWnd, uint msg, nint wParam, nint lParam);

    private const uint WmNcLButtonDown = 0x00A1;
    private const nint HtCaption = 2;

    private void Card_PointerPressed(object sender, PointerRoutedEventArgs e)
    {
        var hwnd = WinRT.Interop.WindowNative.GetWindowHandle(this);
        ReleaseCapture();
        SendMessage(hwnd, WmNcLButtonDown, HtCaption, 0);
    }

    // --- Mic toggle, backed by a real VoiceViewModel (its own session: HudSessionId) ---

    private async void MicButton_Tapped(object sender, TappedRoutedEventArgs e)
    {
        if (VoiceViewModel.IsActive)
        {
            VoiceViewModel.Stop();
        }
        else
        {
            await VoiceViewModel.StartAsync();
        }
        UpdateVoiceVisuals();
    }

    private void UpdateVoiceVisuals()
    {
        var active = VoiceViewModel.IsActive;
        MicButton.Stroke = new Microsoft.UI.Xaml.Media.SolidColorBrush(active ? Colors.Cyan : Colors.SlateGray);
        StateLabel.Text = active ? VoiceViewModel.State switch
        {
            VoiceState.Listening => "LISTENING",
            VoiceState.Armed => "GO AHEAD",
            VoiceState.Thinking => "WORKING",
            VoiceState.Speaking => "SPEAKING",
            _ => "OFFLINE",
        } : "OFFLINE";

        var scale = 1.0 + Math.Min(1.0, VoiceViewModel.Level) * 0.35;
        MicButton.RenderTransform = new Microsoft.UI.Xaml.Media.ScaleTransform { ScaleX = scale, ScaleY = scale, CenterX = 26, CenterY = 26 };
    }

    private async Task HandleCommandAsync(string query)
    {
        var response = await SendAsync(query);
        if (!string.IsNullOrWhiteSpace(response.Spoken ?? response.Response))
        {
            await VoiceViewModel.SpeakAsync(response.Spoken ?? response.Response);
        }
    }

    // --- Telemetry (4s poll, matching useAwareness's HUD interval) ---

    private void StartTelemetry()
    {
        _telemetryCts = new CancellationTokenSource();
        _ = TelemetryLoopAsync(_telemetryCts.Token);
    }

    private void StopTelemetry() => _telemetryCts?.Cancel();

    private async Task TelemetryLoopAsync(CancellationToken ct)
    {
        try
        {
            while (!ct.IsCancellationRequested)
            {
                await PollTelemetryAsync(ct).ConfigureAwait(false);
                await Task.Delay(TimeSpan.FromSeconds(4), ct).ConfigureAwait(false);
            }
        }
        catch (OperationCanceledException) { }
    }

    private async Task PollTelemetryAsync(CancellationToken ct)
    {
        try
        {
            var status = await _api.FetchAwarenessStatusAsync(ct).ConfigureAwait(false);
            DispatcherQueue.TryEnqueue(() => ApplySnapshot(status.Snapshot));
        }
        catch
        {
            DispatcherQueue.TryEnqueue(() => StateLabel.Text = "OFFLINE");
        }
    }

    private void ApplySnapshot(AwarenessSnapshot snapshot)
    {
        VramRing.Text = $"VRAM {snapshot.VramUtilPercent:0}%";
        GpuRing.Text = $"GPU {snapshot.GpuUtilPercent:0}%";
        VramRing.Foreground = new Microsoft.UI.Xaml.Media.SolidColorBrush(snapshot.VramUtilPercent >= 88 ? Colors.OrangeRed : Colors.Cyan);
        GpuRing.Foreground = new Microsoft.UI.Xaml.Media.SolidColorBrush(snapshot.GpuUtilPercent >= 90 ? Colors.Orange : Colors.Gold);
    }

    /// <summary>Non-streaming chat turn for the HUD — mirrors sendChatApi() in api.ts. The
    /// LISTENING/GO AHEAD/WORKING/SPEAKING state label is driven by VoiceViewModel.State via
    /// UpdateVoiceVisuals, not set directly here.</summary>
    public async Task<ChatResponse> SendAsync(string message, CancellationToken ct = default)
    {
        var response = await _api.SendChatAsync(new SendChatRequest
        {
            Message = message,
            SessionId = HudSessionId,
        }, ct).ConfigureAwait(false);
        DispatcherQueue.TryEnqueue(() => CaptionText.Text = response.Response);
        return response;
    }
}
