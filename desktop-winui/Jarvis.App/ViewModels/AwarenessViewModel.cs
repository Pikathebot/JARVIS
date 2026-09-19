using System.Collections.ObjectModel;
using CommunityToolkit.Mvvm.ComponentModel;
using Jarvis.Core.Api;
using Jarvis.Core.Models;
using Jarvis.Core.Sse;
using Microsoft.UI.Dispatching;

namespace Jarvis_App.ViewModels;

/// <summary>
/// Port of the retired Next.js client's src/hooks/useAwareness.ts. Connects to GET /api/awareness/stream and keeps
/// at most 4 visible observation cards, matching the AwarenessTray dedupe rule: a newer observation
/// of the same "kind" replaces the older one; "resolved" removes it. Reconnects on stream failure
/// and backfills by seq (never timestamp — see backend/app/awareness/observations.py).
/// </summary>
public partial class AwarenessViewModel : ObservableObject, IDisposable
{
    private const int MaxVisible = 4;

    private readonly JarvisApiClient _api;
    private readonly AwarenessStreamClient _stream;
    private readonly DispatcherQueue _dispatcher;
    private CancellationTokenSource? _cts;
    private long _latestSeq;

    public ObservableCollection<Observation> Observations { get; } = new();

    [ObservableProperty]
    public partial AwarenessSnapshot? Snapshot { get; set; }

    [ObservableProperty]
    public partial bool Connected { get; set; }

    public event Action<Observation, bool>? ObservationReceived;

    public AwarenessViewModel(JarvisApiClient api, AwarenessStreamClient stream, DispatcherQueue dispatcher)
    {
        _api = api;
        _stream = stream;
        _dispatcher = dispatcher;
    }

    public void Start()
    {
        _cts = new CancellationTokenSource();
        _ = RunAsync(_cts.Token);
    }

    public void Stop() => _cts?.Cancel();

    private async Task RunAsync(CancellationToken ct)
    {
        // Backfill anything missed since the last known seq (0 on first run) before live-streaming.
        try
        {
            var page = await _api.FetchObservationsAsync(_latestSeq, 20, ct).ConfigureAwait(false);
            _dispatcher.TryEnqueue(() =>
            {
                foreach (var obs in page.Observations)
                {
                    Apply(obs);
                }
                _latestSeq = page.LatestSeq;
            });
        }
        catch
        {
            // best-effort; the live stream's "ready" event carries current status too
        }

        while (!ct.IsCancellationRequested)
        {
            try
            {
                await foreach (var evt in _stream.ConnectAsync(ct).ConfigureAwait(false))
                {
                    _dispatcher.TryEnqueue(() =>
                    {
                        Connected = true;
                        switch (evt)
                        {
                            case AwarenessReadyEvent ready:
                                Snapshot = ready.Status.Snapshot;
                                break;
                            case AwarenessObservationEvent obs:
                                Apply(obs.Observation);
                                _latestSeq = Math.Max(_latestSeq, obs.Observation.Seq);
                                ObservationReceived?.Invoke(obs.Observation, obs.Speak);
                                break;
                        }
                    });
                }
            }
            catch (OperationCanceledException)
            {
                break;
            }
            catch
            {
                _dispatcher.TryEnqueue(() => Connected = false);
                try { await Task.Delay(TimeSpan.FromSeconds(3), ct).ConfigureAwait(false); }
                catch (OperationCanceledException) { break; }
            }
        }
    }

    private void Apply(Observation observation)
    {
        if (observation.Resolved)
        {
            var existing = Observations.FirstOrDefault(o => o.Kind == observation.Kind);
            if (existing is not null) Observations.Remove(existing);
            return;
        }

        var sameKind = Observations.FirstOrDefault(o => o.Kind == observation.Kind);
        if (sameKind is not null)
        {
            Observations.Remove(sameKind);
        }

        Observations.Insert(0, observation);
        while (Observations.Count > MaxVisible)
        {
            Observations.RemoveAt(Observations.Count - 1);
        }
    }

    public void Dismiss(Observation observation) => Observations.Remove(observation);

    public void DismissAll() => Observations.Clear();

    public void Dispose()
    {
        _cts?.Cancel();
        _cts?.Dispose();
    }
}
