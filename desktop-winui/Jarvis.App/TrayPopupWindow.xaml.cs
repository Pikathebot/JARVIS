using System.Runtime.InteropServices;
using Jarvis.Core.Api;
using Jarvis.Core.Models;
using Jarvis_App.ViewModels;
using Microsoft.UI;
using Microsoft.UI.Dispatching;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Media;

namespace Jarvis_App;

/// <summary>
/// The tray icon's right-click popup (Steam-style, raised by the user 2026-10-20; design canvas
/// "Jarvis Tray Popup"). Replaces the native context menu, which was built once and so could
/// only say "Game mode on/off": this one reads the governor live, names each switch's state,
/// and shows VRAM and RAM. Opens above the cursor, closes when it loses focus or on Esc.
/// </summary>
public sealed partial class TrayPopupWindow : Window
{
    private const double PopupWidth = 320;

    private readonly JarvisApiClient _api;
    private readonly GovernorViewModel _governor;
    private readonly AppWindow _appWindow;
    private readonly DispatcherQueueTimer _timer;
    private bool _syncing;
    private double _knownHeight = 520; // DIPs; corrected after the first layout

    public event Action? OpenRequested;
    public event Action? HudRequested;
    public event Action? SettingsRequested;
    public event Action? QuitRequested;

    public TrayPopupWindow(JarvisApiClient api, GovernorViewModel governor)
    {
        _api = api;
        _governor = governor;
        InitializeComponent();
        Themes.JarvisTheme.Attach(this);
        SystemBackdrop = new DesktopAcrylicBackdrop();
        Root.VerticalAlignment = VerticalAlignment.Top;

        var hwnd = WinRT.Interop.WindowNative.GetWindowHandle(this);
        _appWindow = AppWindow.GetFromWindowId(Win32Interop.GetWindowIdFromWindow(hwnd));
        _appWindow.IsShownInSwitchers = false;
        if (_appWindow.Presenter is OverlappedPresenter presenter)
        {
            // Border without a title bar: Windows 11 gives it the rounded corners, hairline and
            // shadow of its own flyouts.
            presenter.SetBorderAndTitleBar(true, false);
            presenter.IsAlwaysOnTop = true;
            presenter.IsResizable = false;
            presenter.IsMaximizable = false;
            presenter.IsMinimizable = false;
        }
        // Alt+F4 only hides it; Quit closes it for real (CloseForExit).
        _appWindow.Closing += (_, e) =>
        {
            if (_exiting) return;
            e.Cancel = true;
            Dismiss();
        };

        Activated += (_, e) =>
        {
            if (e.WindowActivationState == WindowActivationState.Deactivated) Dismiss();
        };
        // The first real layout gives the true height: fit the window to it.
        Root.SizeChanged += (_, e) =>
        {
            if (Math.Abs(e.NewSize.Height - _knownHeight) < 1) return;
            _knownHeight = e.NewSize.Height;
            if (_appWindow.IsVisible) Place(_anchor);
        };

        _timer = DispatcherQueue.CreateTimer();
        _timer.Interval = TimeSpan.FromSeconds(2);
        _timer.Tick += async (_, _) => await RefreshTelemetryAsync();
        _governor.PropertyChanged += (_, _) => DispatcherQueue.TryEnqueue(UpdateState);
    }

    private Windows.Graphics.PointInt32 _anchor;

    /// <summary>Opens the popup with its bottom-right corner at the cursor (the tray icon).</summary>
    public void ShowAtCursor()
    {
        GetCursorPos(out var cursor);
        _anchor = new Windows.Graphics.PointInt32(cursor.X, cursor.Y);
        UpdateState();
        _ = RefreshTelemetryAsync();
        Place(_anchor);
        _appWindow.Show(true);
        Activate();
        // A tray click hands this process the foreground right; claim it so that clicking
        // anywhere else deactivates (and so closes) the popup.
        SetForegroundWindow(WinRT.Interop.WindowNative.GetWindowHandle(this));
        _timer.Start();
    }

    private bool _exiting;

    public void CloseForExit()
    {
        _exiting = true;
        _timer.Stop();
        Close();
    }

    public void Dismiss()
    {
        _timer.Stop();
        if (_appWindow.IsVisible) _appWindow.Hide();
    }

    private void Place(Windows.Graphics.PointInt32 anchor)
    {
        var scale = GetDpiForWindow(WinRT.Interop.WindowNative.GetWindowHandle(this)) / 96.0;
        if (scale <= 0) scale = 1;
        var width = (int)Math.Round(PopupWidth * scale);
        var height = (int)Math.Round(_knownHeight * scale);
        var work = DisplayArea.GetFromPoint(anchor, DisplayAreaFallback.Nearest).WorkArea;
        var gap = (int)Math.Round(8 * scale);
        // Above and to the left of the cursor; flipped below when the taskbar is at the top.
        var x = Math.Clamp(anchor.X - width, work.X + gap, work.X + work.Width - width - gap);
        var y = anchor.Y - height - gap;
        if (y < work.Y) y = Math.Min(anchor.Y + gap, work.Y + work.Height - height - gap);
        y = Math.Max(work.Y + gap, Math.Min(y, work.Y + work.Height - height - gap));
        _appWindow.MoveAndResize(new Windows.Graphics.RectInt32(x, y, width, height));
    }

    private void UpdateState()
    {
        var game = _governor.GameMode;
        var online = _governor.Status != "offline";
        var model = MainWindow.ShortModelName(_governor.ConfiguredModel);

        (string text, string token) status = !online ? ("Offline · the backend isn't answering", "StatusError")
            : game ? ("Game mode · no model loaded", "StatusWarning")
            : _governor.Status == "ok" ? ($"Ready · {(model.Length > 0 ? model : "no model")}", "StatusSuccess")
            : (_governor.Status == "throttled" ? "Throttled · high load" : "Degraded", "StatusWarning");
        StatusText.Text = status.text;
        StatusDot.Fill = Themes.JarvisTheme.Brush(status.token);

        _syncing = true;
        try
        {
            GameToggle.IsOn = game;
            GameToggle.IsEnabled = online;
            GovToggle.IsOn = _governor.GovernorAuto;
            GovToggle.IsEnabled = online && !game;
        }
        finally
        {
            _syncing = false;
        }
        GameIcon.Foreground = Themes.JarvisTheme.Brush(game ? "StatusWarning" : "LabelSecondary");
        GameSub.Text = game ? "On · the GPU is free for your game" : "Off · unloads the model for games";
        GovSub.Text = game ? "Paused while Game mode is on"
            : _governor.GovernorAuto ? "Auto · yields VRAM to other apps" : "Off · ignores load";
        FreeVramButton.IsEnabled = online && !game;
        FreeVramHint.Text = game ? "Nothing loaded" : "";
    }

    private async Task RefreshTelemetryAsync()
    {
        if (_governor.Status == "offline") return;
        AwarenessSnapshot snapshot;
        try { snapshot = (await _api.FetchAwarenessStatusAsync()).Snapshot; }
        catch { return; }
        DispatcherQueue.TryEnqueue(() => ApplyTelemetry(snapshot));
    }

    private void ApplyTelemetry(AwarenessSnapshot s)
    {
        if (s.GpuAvailable && s.VramTotalMb > 0)
        {
            VramText.Text = $"{s.VramUsedMb / 1024.0:0.0} / {s.VramTotalMb / 1024.0:0.0} GB";
            SetMeter(VramTrack, VramFill, s.VramUsedMb / s.VramTotalMb);
            VramFill.Background = Themes.JarvisTheme.Brush(s.VramUtilPercent >= 88 ? "MeterHigh" : "MeterNormal");
        }
        else
        {
            VramText.Text = "No GPU";
            SetMeter(VramTrack, VramFill, 0);
        }
        if (s.RamTotalMb > 0)
        {
            RamText.Text = $"{s.RamUsedMb / 1024.0:0.0} / {s.RamTotalMb / 1024.0:0.0} GB";
            SetMeter(RamTrack, RamFill, s.RamUsedMb / s.RamTotalMb);
        }
    }

    private static void SetMeter(FrameworkElement track, FrameworkElement fill, double fraction) =>
        fill.Width = Math.Max(0, track.ActualWidth > 0 ? track.ActualWidth : PopupWidth - 56) * Math.Clamp(fraction, 0, 1);

    private async void GameToggle_Toggled(object sender, RoutedEventArgs e)
    {
        if (_syncing) return;
        try { await _governor.SetGameModeAsync(GameToggle.IsOn); }
        catch { /* the next poll shows the truth */ }
        _governor.PollNow();
    }

    private async void GovToggle_Toggled(object sender, RoutedEventArgs e)
    {
        if (_syncing) return;
        try { await _governor.SetGovernorAutoAsync(GovToggle.IsOn); }
        catch { /* the next poll shows the truth */ }
        _governor.PollNow();
    }

    private void Open_Click(object sender, RoutedEventArgs e) { Dismiss(); OpenRequested?.Invoke(); }
    private void Hud_Click(object sender, RoutedEventArgs e) { Dismiss(); HudRequested?.Invoke(); }
    private void Settings_Click(object sender, RoutedEventArgs e) { Dismiss(); SettingsRequested?.Invoke(); }
    private void Quit_Click(object sender, RoutedEventArgs e) { Dismiss(); QuitRequested?.Invoke(); }

    private async void FreeVram_Click(object sender, RoutedEventArgs e)
    {
        try { await _api.UnloadModelsAsync(); } catch { /* surfaced via the governor poll */ }
        _governor.PollNow();
    }

    private void Root_KeyDown(object sender, KeyRoutedEventArgs e)
    {
        if (e.Key == Windows.System.VirtualKey.Escape) { e.Handled = true; Dismiss(); }
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct POINT { public int X; public int Y; }

    [DllImport("user32.dll")]
    private static extern bool GetCursorPos(out POINT point);

    [DllImport("user32.dll")]
    private static extern bool SetForegroundWindow(IntPtr hwnd);

    [DllImport("user32.dll")]
    private static extern uint GetDpiForWindow(IntPtr hwnd);
}
