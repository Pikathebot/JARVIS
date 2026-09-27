using System.Runtime.InteropServices;
using Jarvis.Core.Api;
using Jarvis.Core.Models;
using Jarvis.Core.Voice;
using Jarvis_App.ViewModels;
using Microsoft.UI;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Input;

namespace Jarvis_App;

/// <summary>
/// Port of the retired Next.js client's src/app/hud/page.tsx + the window-management half of
/// the retired Tauri shell's main.rs. 460x108-ish, borderless, always-on-top, no taskbar entry,
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

    /// <summary>Actions the last HUD turn asked approval for. The HUD has no card to click, so
    /// the spoken prompt ("say yes to proceed, or no to cancel") is the only way to answer, and
    /// the next utterance is read against this first.</summary>
    private readonly List<PendingConfirmation> _pendingConfirmations = new();

    /// <summary>The prompt that led to the pending ask; an approval resubmits it with the ids
    /// attached, since the backend rejects an empty message.</summary>
    private string _lastQuery = "";
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
        MicButton.Stroke = Themes.JarvisTheme.Brush(active ? "Accent" : "LabelSecondary");
        StateLabel.Text = active ? VoiceViewModel.State switch
        {
            VoiceState.Listening => "Listening",
            VoiceState.Armed => "Go ahead",
            VoiceState.Thinking => "Working",
            VoiceState.Speaking => "Speaking",
            _ => "Offline",
        } : "Offline";
        StateLabel.Foreground = Themes.JarvisTheme.Brush(active ? "Label" : "LabelSecondary");

        var scale = 1.0 + Math.Min(1.0, VoiceViewModel.Level) * 0.35;
        MicButton.RenderTransform = new Microsoft.UI.Xaml.Media.ScaleTransform { ScaleX = scale, ScaleY = scale, CenterX = 26, CenterY = 26 };
    }

    private async Task HandleCommandAsync(string query)
    {
        List<string>? approved = null;
        if (_pendingConfirmations.Count > 0)
        {
            switch (ConfirmationIntentParser.Parse(query))
            {
                case ConfirmationIntent.Yes:
                    approved = _pendingConfirmations.Select(p => p.ActionId).ToList();
                    _pendingConfirmations.Clear();
                    query = _lastQuery;
                    break;
                case ConfirmationIntent.No:
                    var denied = _pendingConfirmations.ToList();
                    _pendingConfirmations.Clear();
                    DispatcherQueue.TryEnqueue(() => CaptionText.Text = "Cancelled.");
                    _ = Task.Run(async () =>
                    {
                        foreach (var confirmation in denied)
                        {
                            try { await _api.DenyConfirmationAsync(confirmation.ActionId).ConfigureAwait(false); }
                            catch { /* best-effort; the backend's timeout is the fallback */ }
                        }
                    });
                    await VoiceViewModel.SpeakAsync("Understood, cancelled.");
                    return;
                // Anything else is an ordinary question: it goes to the model with nothing
                // approved, and the pending action waits for a real answer or the timeout.
            }
        }

        if (approved is null)
        {
            _lastQuery = query;
        }
        var response = await SendAsync(query, approved);
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
            DispatcherQueue.TryEnqueue(() => StateLabel.Text = "Offline");
        }
    }

    private void ApplySnapshot(AwarenessSnapshot snapshot)
    {
        // Meters: normal under the soft limit, warning past it -- and the word says so too.
        var vramHigh = snapshot.VramUtilPercent >= 88;
        var gpuHigh = snapshot.GpuUtilPercent >= 90;
        VramRing.Text = $"VRAM {snapshot.VramUtilPercent:0}%" + (vramHigh ? " · high" : "");
        GpuRing.Text = $"GPU {snapshot.GpuUtilPercent:0}%" + (gpuHigh ? " · busy" : "");
        VramRing.Foreground = Themes.JarvisTheme.Brush(vramHigh ? "MeterHigh" : "LabelSecondary");
        GpuRing.Foreground = Themes.JarvisTheme.Brush(gpuHigh ? "MeterHigh" : "LabelSecondary");
    }

    /// <summary>Non-streaming chat turn for the HUD — mirrors sendChatApi() in api.ts. The
    /// LISTENING/GO AHEAD/WORKING/SPEAKING state label is driven by VoiceViewModel.State via
    /// UpdateVoiceVisuals, not set directly here.</summary>
    public async Task<ChatResponse> SendAsync(string message, List<string>? approvedActionIds = null, CancellationToken ct = default)
    {
        var response = await _api.SendChatAsync(new SendChatRequest
        {
            Message = message,
            SessionId = HudSessionId,
            ApprovedActionIds = approvedActionIds is { Count: > 0 } ? approvedActionIds : null,
        }, ct).ConfigureAwait(false);
        // A turn that asks for approval replaces whatever was pending: the backend re-evaluates
        // the same tool call on resubmission and its earlier token is no longer the one to send.
        _pendingConfirmations.Clear();
        if (response.PendingConfirmations is { Count: > 0 })
        {
            _pendingConfirmations.AddRange(response.PendingConfirmations);
        }
        DispatcherQueue.TryEnqueue(() => CaptionText.Text = response.Response);
        return response;
    }
}
