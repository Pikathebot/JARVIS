using System.Collections.ObjectModel;
using CommunityToolkit.Mvvm.ComponentModel;
using Jarvis.Core.Api;
using Jarvis.Core.Models;
using Microsoft.UI.Dispatching;

namespace Jarvis_App.ViewModels;

/// <summary>
/// "Get models" in Settings: the curated list, HuggingFace search, a repo's files with fit
/// labels, and the download queue (backend/app/agent/model_hub.py).
///
/// Owned by <see cref="ModelsViewModel"/> rather than the Settings sheet, so a download keeps
/// being watched after the sheet closes, and a finished one refreshes the model list.
/// </summary>
public partial class ModelHubViewModel : ObservableObject
{
    private readonly JarvisApiClient _api;
    private readonly DispatcherQueue _dispatcher;
    private readonly Func<Task> _onDownloaded;
    private readonly DispatcherQueueTimer _poll;
    private List<HubFileInfo> _repoProjectors = new();

    public ObservableCollection<CuratedModel> Curated { get; } = new();
    public ObservableCollection<HubSearchResult> SearchResults { get; } = new();
    public ObservableCollection<HubFileInfo> RepoFiles { get; } = new();
    public ObservableCollection<DownloadItem> Downloads { get; } = new();

    /// <summary>The repo whose files are listed, or null.</summary>
    [ObservableProperty]
    public partial string? OpenRepo { get; set; }

    /// <summary>Whether the open repo has a projector to download alongside.</summary>
    [ObservableProperty]
    public partial bool RepoHasVision { get; set; }

    /// <summary>Download the repo's projector beside the model (vision). On by default when
    /// the repo has one.</summary>
    [ObservableProperty]
    public partial bool IncludeVision { get; set; } = true;

    [ObservableProperty]
    public partial bool IsBusy { get; set; }

    [ObservableProperty]
    public partial string? StatusMessage { get; set; }

    /// <summary>"Your card can give a model 5.9 GB right now" -- the number every label is
    /// measured against.</summary>
    [ObservableProperty]
    public partial string? BudgetText { get; set; }

    public ModelHubViewModel(JarvisApiClient api, DispatcherQueue dispatcher, Func<Task> onDownloaded)
    {
        _api = api;
        _dispatcher = dispatcher;
        _onDownloaded = onDownloaded;
        _poll = dispatcher.CreateTimer();
        _poll.Interval = TimeSpan.FromSeconds(1);
        _poll.Tick += async (_, _) => await RefreshDownloadsAsync();
    }

    partial void OnIncludeVisionChanged(bool value) => ShowRepoFiles(RepoFiles.ToList());

    public async Task LoadCuratedAsync()
    {
        await RunAsync("Loading the curated list…", async () =>
        {
            var curated = await _api.FetchCuratedModelsAsync().ConfigureAwait(false);
            _dispatcher.TryEnqueue(() =>
            {
                Curated.Clear();
                foreach (var model in curated.Models) Curated.Add(model);
                SetBudget(curated.FitBudgetMb);
            });
        });
        await RefreshDownloadsAsync();
    }

    public async Task SearchAsync(string query)
    {
        query = query.Trim();
        if (query.Length < 2) return;
        await RunAsync($"Searching HuggingFace for “{query}”…", async () =>
        {
            var found = await _api.SearchHubAsync(query).ConfigureAwait(false);
            _dispatcher.TryEnqueue(() =>
            {
                SearchResults.Clear();
                foreach (var result in found.Results) SearchResults.Add(result);
                StatusMessage = found.Results.Count == 0 ? $"No GGUF repos match “{query}”." : null;
            });
        }, keepStatus: true);
    }

    public async Task OpenRepoAsync(string repo)
    {
        await RunAsync($"Reading {repo} (the first look at a repo takes a few seconds)…", async () =>
        {
            var files = await _api.FetchHubFilesAsync(repo).ConfigureAwait(false);
            _dispatcher.TryEnqueue(() =>
            {
                OpenRepo = files.Repo;
                _repoProjectors = files.Projectors;
                RepoHasVision = files.Projectors.Count > 0;
                SetBudget(files.FitBudgetMb);
                ShowRepoFiles(files.Models);
            });
        });
    }

    public void CloseRepo()
    {
        OpenRepo = null;
        RepoFiles.Clear();
        _repoProjectors = new();
        RepoHasVision = false;
    }

    public Task DownloadCuratedAsync(CuratedModel model) =>
        StartAsync(new ModelDownloadRequest { Repo = model.Repo, File = model.File.Name, Projector = model.Projector?.Name });

    public Task DownloadRepoFileAsync(HubFileInfo file)
    {
        if (OpenRepo is null) return Task.CompletedTask;
        // The smallest projector: they encode the same vision tower at different precisions.
        var projector = IncludeVision ? _repoProjectors.OrderBy(p => p.SizeBytes).FirstOrDefault() : null;
        return StartAsync(new ModelDownloadRequest { Repo = OpenRepo, File = file.Name, Projector = projector?.Name });
    }

    public async Task CancelAsync(DownloadItem item)
    {
        try
        {
            await _api.CancelModelDownloadAsync(item.Id).ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            _dispatcher.TryEnqueue(() => StatusMessage = $"Could not cancel: {ex.Message}");
        }
        await RefreshDownloadsAsync();
    }

    private async Task StartAsync(ModelDownloadRequest request)
    {
        try
        {
            await _api.StartModelDownloadAsync(request).ConfigureAwait(false);
            _dispatcher.TryEnqueue(() => StatusMessage = null);
        }
        catch (Exception ex)
        {
            _dispatcher.TryEnqueue(() => StatusMessage = $"Could not start the download: {ex.Message}");
        }
        await RefreshDownloadsAsync();
    }

    /// <summary>Polled once a second while anything is in flight; stops itself after.</summary>
    public async Task RefreshDownloadsAsync()
    {
        ModelDownloadsResponse jobs;
        try
        {
            jobs = await _api.FetchModelDownloadsAsync().ConfigureAwait(false);
        }
        catch
        {
            return; // backend restarting: the next tick (or the next open) tries again
        }

        var finished = false;
        _dispatcher.TryEnqueue(() =>
        {
            foreach (var job in jobs.Downloads)
            {
                var item = Downloads.FirstOrDefault(d => d.Id == job.Id);
                if (item is null)
                {
                    item = new DownloadItem(job.Id);
                    Downloads.Insert(0, item);
                }
                finished |= item.Update(job);
            }

            if (jobs.Downloads.Any(j => j.IsActive))
            {
                if (!_poll.IsRunning) _poll.Start();
            }
            else
            {
                _poll.Stop();
            }

            if (finished)
            {
                _ = AfterDownloadAsync();
            }
        });
    }

    /// <summary>A model just landed: list it in the picker and flip its Download buttons.</summary>
    private async Task AfterDownloadAsync()
    {
        await _onDownloaded().ConfigureAwait(false);
        await LoadCuratedAsync().ConfigureAwait(false);
        var repo = OpenRepo;
        if (repo is not null) await OpenRepoAsync(repo).ConfigureAwait(false);
    }

    private void ShowRepoFiles(List<HubFileInfo> files)
    {
        RepoFiles.Clear();
        foreach (var file in files)
        {
            file.WithVision = IncludeVision && RepoHasVision;
            RepoFiles.Add(file);
        }
    }

    private void SetBudget(double? budgetMb)
    {
        BudgetText = budgetMb is null
            ? "Labels need a GPU reading; they show “unknown” until the backend has one."
            : $"Your card can give a model {budgetMb.Value / 1024:0.0} GB right now (the rest is what other apps have held lately). "
              + "Fits leaves 500 MB spare; Spills would run partly from shared memory, slowly.";
    }

    private async Task RunAsync(string busyText, Func<Task> work, bool keepStatus = false)
    {
        _dispatcher.TryEnqueue(() =>
        {
            IsBusy = true;
            StatusMessage = busyText;
        });
        try
        {
            await work().ConfigureAwait(false);
            if (!keepStatus) _dispatcher.TryEnqueue(() => StatusMessage = null);
        }
        catch (Exception ex)
        {
            _dispatcher.TryEnqueue(() => StatusMessage = ex.Message);
        }
        finally
        {
            _dispatcher.TryEnqueue(() => IsBusy = false);
        }
    }
}

/// <summary>One row of the download list, updated in place as the job progresses.</summary>
public partial class DownloadItem : ObservableObject
{
    public string Id { get; }

    [ObservableProperty]
    public partial string Title { get; set; } = "";

    [ObservableProperty]
    public partial double Progress { get; set; }

    [ObservableProperty]
    public partial string Detail { get; set; } = "";

    [ObservableProperty]
    public partial bool CanCancel { get; set; }

    private string _status = "";

    public DownloadItem(string id) => Id = id;

    /// <summary>Takes a fresh job snapshot; true the first time it is seen as done.</summary>
    public bool Update(ModelDownloadJob job)
    {
        var justFinished = job.Status == "done" && _status is not ("" or "done");
        _status = job.Status;

        Title = job.Files.FirstOrDefault() ?? job.Repo;
        Progress = job.BytesTotal > 0 ? 100.0 * job.BytesDone / job.BytesTotal : 0;
        CanCancel = job.IsActive;
        var amount = $"{SizeFormat.Bytes(job.BytesDone)} of {SizeFormat.Bytes(job.BytesTotal)}";
        Detail = job.Status switch
        {
            "queued" => $"Waiting for the download before it · {job.Repo}",
            "downloading" => $"{amount} · {job.CurrentFile}",
            "verifying" => $"Checking {job.CurrentFile}…",
            "done" => $"Downloaded to {job.DestDir} · pick it under Models",
            "failed" => $"Failed: {job.Error}",
            "cancelled" => $"Cancelled at {amount} · Download again to resume",
            _ => job.Status,
        };
        return justFinished;
    }
}
