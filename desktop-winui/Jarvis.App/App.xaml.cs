using Jarvis.Core.Api;
using Jarvis.Core.Sse;
using Jarvis_App.Services;
using Jarvis_Glass;
using Microsoft.UI.Xaml;

namespace Jarvis_App;

/// <summary>
/// Application entry point and the full native shell: single-instance guard, backend
/// supervision, tray icon, global hotkey, main window, and HUD window. Replaces the retired
/// run_jarvis.py launcher and the pywebview/Tauri UIs it juggled.
/// </summary>
public partial class App : Application
{
    public static Window Window { get; private set; } = null!;

    public static Microsoft.UI.Dispatching.DispatcherQueue DispatcherQueue { get; private set; } = null!;

    public static nint WindowHandle =>
        WinRT.Interop.WindowNative.GetWindowHandle(Window);

    private JarvisApiClient _api = null!;
    private BackendHost _backendHost = null!;
    private TrayService? _tray;
    private MainWindow _mainWindow = null!;
    private HudWindow _hudWindow = null!;

    public App()
    {
        InitializeComponent();
        UnhandledException += (_, e) =>
        {
            LogCrash(e.Exception);
            e.Handled = true; // keep the process alive with whatever window state we have, rather than a silent exit
        };
        AppDomain.CurrentDomain.UnhandledException += (_, e) => LogCrash(e.ExceptionObject as Exception);
    }

    protected override async void OnLaunched(LaunchActivatedEventArgs args)
    {
        try
        {
            if (!SingleInstanceService.ClaimOrRedirect())
            {
                // Another instance is already running and just received our activation — exit quietly.
                Microsoft.UI.Xaml.Application.Current.Exit();
                return;
            }

            DispatcherQueue = Microsoft.UI.Dispatching.DispatcherQueue.GetForCurrentThread();

            var httpClient = new HttpClient { BaseAddress = new Uri(JarvisApiClient.DefaultBaseUrl), Timeout = TimeSpan.FromSeconds(30) };
            // /chat/stream can legitimately run long — its own request bypasses this via a separate
            // HttpClient instance so a slow model doesn't also stall unrelated polling calls.
            var streamHttpClient = new HttpClient { BaseAddress = new Uri(JarvisApiClient.DefaultBaseUrl), Timeout = Timeout.InfiniteTimeSpan };

            _api = new JarvisApiClient(httpClient);
            var chatStreamClient = new ChatStreamClient(streamHttpClient);
            var awarenessStreamClient = new AwarenessStreamClient(streamHttpClient);

            var repoRoot = FindRepoRoot();
            _backendHost = new BackendHost(_api, repoRoot);

            _mainWindow = new MainWindow(_api, chatStreamClient, awarenessStreamClient);
            _hudWindow = new HudWindow(_api);
            _mainWindow.Hud = _hudWindow;
            WindowPositionService.Register(_mainWindow);
            WindowPositionService.Register(_hudWindow);
            WindowCaptureExclusion.Register(_mainWindow);
            WindowCaptureExclusion.Register(_hudWindow);
            Window = _mainWindow;

            SetupTray();

            Window.Activate();

            var ready = await _backendHost.EnsureRunningAsync(TimeSpan.FromSeconds(20)).ConfigureAwait(true);
            if (!ready)
            {
                // The governor pill / health poll already surfaces "Offline"; nothing further to do
                // here beyond letting the user retry once the backend comes up on its own.
            }
        }
        catch (Exception ex)
        {
            LogCrash(ex);
        }
    }

    private static void LogCrash(Exception? ex)
    {
        try
        {
            var path = Path.Combine(AppContext.BaseDirectory, "jarvis-app-crash.log");
            File.AppendAllText(path, $"[{DateTimeOffset.Now:O}]\n{ex}\n\n");
        }
        catch
        {
            // last resort — nothing more we can do
        }
    }

    private void SetupTray()
    {
        var iconPath = Path.Combine(AppContext.BaseDirectory, "Assets", "AppIcon.ico");
        if (!File.Exists(iconPath))
        {
            return;
        }

        // H.NotifyIcon's IconSource resolves through StorageFile.GetFileFromApplicationUriAsync,
        // which only accepts package-relative schemes (ms-appx://) — a plain file:// URI to the
        // same path throws ERROR_INVALID_PARAMETER since that API is not a general file opener.
        _tray = new TrayService("ms-appx:///Assets/AppIcon.ico");
        _tray.ShowRequested += () => DispatcherQueue.TryEnqueue(() =>
        {
            _mainWindow.Activate();
        });
        _tray.ToggleHudRequested += () => DispatcherQueue.TryEnqueue(() => _hudWindow.ToggleVisible());
        _tray.FreeVramRequested += async () =>
        {
            try { await _api.UnloadModelsAsync(); } catch { /* best-effort */ }
        };
        _tray.ExitRequested += () => DispatcherQueue.TryEnqueue(() =>
        {
            _backendHost.Dispose();
            Microsoft.UI.Xaml.Application.Current.Exit();
        });
    }

    /// <summary>
    /// Walks up from the app's build output to find the JARVIS repo root (marked by
    /// backend/app/main.py and a desktop-winui/ directory), so BackendHost can locate .venv and
    /// backend/ regardless of whether we're running from bin\Debug\... during development or a
    /// published layout.
    /// </summary>
    private static string FindRepoRoot()
    {
        var dir = new DirectoryInfo(AppContext.BaseDirectory);
        while (dir is not null)
        {
            if (File.Exists(Path.Combine(dir.FullName, "backend", "app", "main.py")) &&
                Directory.Exists(Path.Combine(dir.FullName, "desktop-winui")))
            {
                return dir.FullName;
            }
            dir = dir.Parent;
        }

        // Fallback: assume desktop-winui is a direct child of the repo root (its normal position).
        return Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "..", "..", "..", "..", ".."));
    }
}
