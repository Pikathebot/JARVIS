using Jarvis.Core.Api;
using Jarvis.Core.Sse;
using Jarvis_App.Services;
using Jarvis_Glass;
using Microsoft.UI.Xaml;
using Microsoft.Windows.AppLifecycle;

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
    private SnapshotService? _snapshots;
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

            // The backend first: uvicorn takes seconds to import and start, and building the
            // windows needn't come before it.
            var repoRoot = FindRepoRoot();
            _backendHost = new BackendHost(_api, repoRoot);
            var backendReady = _backendHost.EnsureRunningAsync(TimeSpan.FromSeconds(20));
            LogStartup("backend launched");

            _mainWindow = new MainWindow(_api, chatStreamClient, awarenessStreamClient);
            _hudWindow = new HudWindow(_api);
            _mainWindow.Hud = _hudWindow;
            WindowPositionService.Register(_mainWindow);
            WindowPositionService.Register(_hudWindow);
            // Keep capture exclusion ON. Turning it off (tried for scripted screenshots) makes
            // the live glass re-capture its own output, so the scene never settles: the renderer
            // ran every frame at ~70% GPU, the governor throttled and evicted the model.
            WindowCaptureExclusion.Enabled = true;
            WindowCaptureExclusion.Register(_mainWindow);
            WindowCaptureExclusion.Register(_hudWindow);
            Window = _mainWindow;
            // Dev screenshots with the glass in them (scripts/snapshot-window.ps1).
            _snapshots = new SnapshotService(DispatcherQueue, new[]
            {
                ("main", WinRT.Interop.WindowNative.GetWindowHandle(_mainWindow)),
                ("hud", WinRT.Interop.WindowNative.GetWindowHandle(_hudWindow)),
            });

            SetupTray();
            // X hides to the tray only while there is a tray icon to come back through; without
            // one a real close must end the app rather than leave it running with no window.
            _mainWindow.HideOnClose = _tray is not null;
            _mainWindow.Closed += (_, _) => ExitApp();
            // A second launch is redirected here by SingleInstanceService; show the window for it.
            AppInstance.GetCurrent().Activated += (_, _) =>
                DispatcherQueue.TryEnqueue(() => _mainWindow.ShowFromBackground());

            Window.Activate();
            LogStartup("window shown");

            var ready = await backendReady.ConfigureAwait(true);
            LogStartup(ready ? "backend ready" : "backend did not come up");
            if (ready)
            {
                _mainWindow.GovernorViewModel.PollNow();
            }
            // Otherwise the governor pill / health poll already surfaces "Offline", and the
            // window opens anyway once the backend comes up on its own.
        }
        catch (Exception ex)
        {
            LogCrash(ex);
        }
    }

    /// <summary>Startup milestones in jarvis-app.log, timed from process start.</summary>
    public static void LogStartup(string milestone) =>
        Log($"startup: {milestone} at {(DateTime.Now - System.Diagnostics.Process.GetCurrentProcess().StartTime).TotalMilliseconds:F0} ms");

    private static void LogCrash(Exception? ex) => Log("jarvis-app-crash.log", $"{ex}\n");

    /// <summary>Appends to jarvis-app.log beside the exe -- for best-effort paths that swallow
    /// their exceptions (sidebar/project refreshes), so a silent failure is at least findable.</summary>
    public static void Log(string message) => Log("jarvis-app.log", message);

    /// <summary>jarvis-voice.log beside the exe: what the microphone path is doing (graph format,
    /// levels, utterances, upload results), since none of it is visible in the UI.</summary>
    public static void LogVoice(string message) => Log("jarvis-voice.log", message);

    private static void Log(string file, string message)
    {
        try
        {
            var path = Path.Combine(AppContext.BaseDirectory, file);
            File.AppendAllText(path, $"[{DateTimeOffset.Now:O}] {message}\n");
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
        _tray.ShowRequested += () => DispatcherQueue.TryEnqueue(() => _mainWindow.ShowFromBackground());
        _tray.ToggleHudRequested += () => DispatcherQueue.TryEnqueue(() => _hudWindow.ToggleVisible());
        _tray.FreeVramRequested += async () =>
        {
            try { await _api.UnloadModelsAsync(); } catch { /* best-effort */ }
        };
        _tray.ExitRequested += () => DispatcherQueue.TryEnqueue(ExitApp);
    }

    private bool _exiting;

    /// <summary>The one way out: stops the backend (and its llama-servers) and ends the process.</summary>
    private void ExitApp()
    {
        if (_exiting) return;
        _exiting = true;
        _mainWindow.HideOnClose = false;
        _snapshots?.Dispose();
        _tray?.Dispose();
        _backendHost.Dispose();
        Microsoft.UI.Xaml.Application.Current.Exit();
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
