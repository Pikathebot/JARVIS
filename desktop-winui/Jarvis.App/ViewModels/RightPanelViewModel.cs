using System.Collections.ObjectModel;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using Jarvis.Core.Api;
using Jarvis.Core.Models;
using Microsoft.UI.Dispatching;

namespace Jarvis_App.ViewModels;

/// <summary>
/// Port of the Artifacts + Files tabs from RightPanel.tsx (579 ln in the original — Context and
/// Activity tabs are not ported yet, see winui_migration_status memory). Artifacts: list, select,
/// version picker, view content. (Files moved to the project page, PLAN 4.18.)
/// </summary>
public partial class RightPanelViewModel : ObservableObject
{
    private readonly JarvisApiClient _api;
    private readonly DispatcherQueue _dispatcher;

    public ObservableCollection<Artifact> Artifacts { get; } = new();
    public ObservableCollection<ArtifactVersion> Versions { get; } = new();

    [ObservableProperty]
    public partial bool IsOpen { get; set; }

    [ObservableProperty]
    public partial string? ProjectId { get; set; }

    [ObservableProperty]
    public partial string? SessionId { get; set; }

    [ObservableProperty]
    public partial Artifact? SelectedArtifact { get; set; }

    [ObservableProperty]
    public partial string SelectedContent { get; set; } = "";

    public RightPanelViewModel(JarvisApiClient api, DispatcherQueue dispatcher)
    {
        _api = api;
        _dispatcher = dispatcher;
    }

    [RelayCommand]
    public void Toggle() => IsOpen = !IsOpen;

    [RelayCommand]
    public async Task RefreshArtifactsAsync()
    {
        try
        {
            var artifacts = await _api.FetchArtifactsAsync(SessionId, ProjectId).ConfigureAwait(false);
            _dispatcher.TryEnqueue(() =>
            {
                Artifacts.Clear();
                foreach (var artifact in artifacts) Artifacts.Add(artifact);
            });
        }
        catch
        {
            // best-effort
        }
    }

    [RelayCommand]
    public async Task SelectArtifactAsync(Artifact artifact)
    {
        SelectedArtifact = artifact;
        SelectedContent = artifact.Content;
        Versions.Clear();

        try
        {
            var versions = await _api.FetchArtifactVersionsAsync(artifact.Id).ConfigureAwait(false);
            _dispatcher.TryEnqueue(() =>
            {
                foreach (var version in versions) Versions.Add(version);
            });
        }
        catch
        {
            // single-version artifacts commonly 404 the versions endpoint — not an error
        }
    }

    [RelayCommand]
    public async Task SelectVersionAsync(ArtifactVersion version)
    {
        // Backend returns full version content already via FetchArtifactVersionsAsync, so this
        // just switches the displayed text — no extra round trip needed.
        SelectedContent = version.Content;
        await Task.CompletedTask;
    }
}
