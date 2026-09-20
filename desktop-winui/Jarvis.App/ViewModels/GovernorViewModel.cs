using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using Jarvis.Core.Api;
using Jarvis.Core.Models;
using Microsoft.UI.Dispatching;

namespace Jarvis_App.ViewModels;

/// <summary>Port of the retired Next.js client's src/hooks/useGovernor.ts — polls GET /health every 2s.</summary>
public partial class GovernorViewModel : ObservableObject, IDisposable
{
    private readonly JarvisApiClient _api;
    private readonly DispatcherQueue _dispatcher;
    private readonly PeriodicTimer _timer = new(TimeSpan.FromSeconds(2));
    private CancellationTokenSource? _cts;

    [ObservableProperty]
    public partial string Status { get; set; } = "offline"; // "ok" | "degraded" | "throttled" | "offline"

    [ObservableProperty]
    public partial bool Throttled { get; set; }

    [ObservableProperty]
    public partial string ActiveBackend { get; set; } = "";

    [ObservableProperty]
    public partial string ConfiguredModel { get; set; } = "models/Qwen3.5-9B-Q4_K_M.gguf";

    [ObservableProperty]
    public partial List<string> AvailableModels { get; set; } = new();

    [ObservableProperty]
    public partial bool LlamaConnected { get; set; }

    [ObservableProperty]
    public partial bool OllamaConnected { get; set; }

    /// <summary>Raised on the UI thread each time the backend goes from unreachable to
    /// reachable -- first launch (the window is built before uvicorn is up) and any restart.
    /// Anything that fetches state at startup should fetch here instead, or it races the
    /// backend and silently stays empty.</summary>
    public event Action? BackendCameOnline;

    private bool _wasOnline;

    public GovernorViewModel(JarvisApiClient api, DispatcherQueue dispatcher)
    {
        _api = api;
        _dispatcher = dispatcher;
    }

    public void Start()
    {
        _cts = new CancellationTokenSource();
        _ = PollLoopAsync(_cts.Token);
    }

    public void StopPolling() => _cts?.Cancel();

    private async Task PollLoopAsync(CancellationToken ct)
    {
        await PollOnceAsync(ct).ConfigureAwait(false);
        try
        {
            while (await _timer.WaitForNextTickAsync(ct).ConfigureAwait(false))
            {
                await PollOnceAsync(ct).ConfigureAwait(false);
            }
        }
        catch (OperationCanceledException)
        {
            // stopping
        }
    }

    private async Task PollOnceAsync(CancellationToken ct)
    {
        try
        {
            var health = await _api.FetchHealthAsync(ct).ConfigureAwait(false);
            _dispatcher.TryEnqueue(() =>
            {
                Apply(health);
                if (!_wasOnline)
                {
                    _wasOnline = true;
                    BackendCameOnline?.Invoke();
                }
            });
        }
        catch
        {
            _dispatcher.TryEnqueue(() =>
            {
                Status = "offline";
                _wasOnline = false;
            });
        }
    }

    private void Apply(HealthResponse health)
    {
        ActiveBackend = health.ActiveBackend;
        ConfiguredModel = health.ConfiguredModel;
        AvailableModels = health.AvailableModels;
        LlamaConnected = health.LlamaConnected;
        OllamaConnected = health.OllamaConnected ?? false;
        Throttled = health.GovernorThrottled;
        Status = health.Status == "degraded" ? "degraded" : Throttled ? "throttled" : "ok";
    }

    [RelayCommand]
    public async Task PauseAsync(string reason)
    {
        await _api.PauseGovernorAsync(reason).ConfigureAwait(false);
    }

    [RelayCommand]
    public async Task ResumeAsync()
    {
        await _api.ResumeGovernorAsync().ConfigureAwait(false);
    }

    public void Dispose()
    {
        _cts?.Cancel();
        _cts?.Dispose();
        _timer.Dispose();
    }
}
