using Jarvis.Core.Api;
using Jarvis_App.ViewModels;
using Jarvis_App.Views;
using Jarvis_Glass;
using Microsoft.UI;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;

namespace Jarvis_App;

/// <summary>
/// Settings in a window of its own (PLAN 4.8b2), replacing the in-window sheet: movable,
/// resizable, full liquid glass with its own <see cref="GlassHost"/> (GPU cost only while it is
/// open), traffic lights, one instance (the main window keeps it and re-activates it), size and
/// position remembered between openings.
/// </summary>
public sealed partial class SettingsWindow : Window
{
    private const string BoundsKey = "SettingsWindowBounds";
    private readonly AppWindow _appWindow;

    public SettingsPane Pane { get; }

    /// <summary>The liquid-glass renderer behind this window.</summary>
    public GlassHost Glass { get; }

    public SettingsWindow(
        JarvisApiClient api,
        GovernorViewModel governor,
        PersonaViewModel persona,
        RoutinesViewModel routines,
        ModelsViewModel models,
        VoiceViewModel voice)
    {
        InitializeComponent();
        var hwnd = WinRT.Interop.WindowNative.GetWindowHandle(this);
        _appWindow = AppWindow.GetFromWindowId(Win32Interop.GetWindowIdFromWindow(hwnd));

        Pane = new SettingsPane(api, governor, persona, routines, models, voice);
        Host.Children.Add(Pane);

        RestoreBounds();
        if (_appWindow.Presenter is OverlappedPresenter presenter)
        {
            presenter.PreferredMinimumWidth = 760;
            presenter.PreferredMinimumHeight = 520;
        }

        // Title bar first (as MainWindow does), so the glass sizes itself to the final client area.
        Pane.AttachWindow(this, _appWindow);
        Glass = new GlassHost(this);
        WindowCaptureExclusion.Register(this);
        WindowCaptureExclusion.SetExcluded(true);
        Themes.JarvisTheme.Attach(this);

        _appWindow.Closing += (_, _) => SaveBounds();
        // Dev screenshots (scripts/snapshot-window.ps1 -Window settings).
        Services.SnapshotService.Current?.Track("settings", hwnd);
        Closed += (_, _) =>
        {
            Pane.Detach();
            Services.SnapshotService.Current?.Untrack(hwnd);
        };
    }

    public void ShowPage(string? page)
    {
        if (page is not null) Pane.ShowPage(page);
        Activate();
    }

    private void RestoreBounds()
    {
        try
        {
            if (Windows.Storage.ApplicationData.Current.LocalSettings.Values[BoundsKey] is string saved)
            {
                var parts = saved.Split(',').Select(int.Parse).ToArray();
                if (parts.Length == 4 && parts[2] >= 760 && parts[3] >= 520 && IsOnScreen(parts[0], parts[1], parts[2]))
                {
                    _appWindow.MoveAndResize(new Windows.Graphics.RectInt32(parts[0], parts[1], parts[2], parts[3]));
                    return;
                }
            }
        }
        catch
        {
            // no saved bounds (or unpackaged): the default below
        }
        _appWindow.Resize(new Windows.Graphics.SizeInt32(1000, 720));
    }

    /// <summary>A saved position on a monitor that has since been unplugged would open the
    /// window off screen; only restore one whose title strip is on some display.</summary>
    private static bool IsOnScreen(int x, int y, int width)
    {
        var area = DisplayArea.GetFromPoint(new Windows.Graphics.PointInt32(x + width / 2, y + 20), DisplayAreaFallback.None);
        return area is not null;
    }

    private void SaveBounds()
    {
        try
        {
            if ((_appWindow.Presenter as OverlappedPresenter)?.State != OverlappedPresenterState.Restored) return;
            var p = _appWindow.Position;
            var s = _appWindow.Size;
            Windows.Storage.ApplicationData.Current.LocalSettings.Values[BoundsKey] = $"{p.X},{p.Y},{s.Width},{s.Height}";
        }
        catch
        {
            // unpackaged: not remembered
        }
    }
}
