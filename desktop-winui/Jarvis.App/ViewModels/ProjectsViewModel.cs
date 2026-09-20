using System.Collections.ObjectModel;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using Jarvis.Core.Api;
using Jarvis.Core.Models;
using Microsoft.UI.Dispatching;

namespace Jarvis_App.ViewModels;

/// <summary>Port of the workspace-switcher half of Sidebar.tsx: list/create/activate/delete projects.</summary>
public partial class ProjectsViewModel : ObservableObject
{
    private readonly JarvisApiClient _api;
    private readonly DispatcherQueue _dispatcher;

    public ObservableCollection<Project> Projects { get; } = new();

    [ObservableProperty]
    public partial Project? ActiveProject { get; set; }

    public event Action<Project?>? ActiveProjectChanged;

    public ProjectsViewModel(JarvisApiClient api, DispatcherQueue dispatcher)
    {
        _api = api;
        _dispatcher = dispatcher;
    }

    [RelayCommand]
    public async Task RefreshAsync()
    {
        try
        {
            var projects = await _api.FetchProjectsAsync().ConfigureAwait(false);
            var active = await _api.FetchActiveProjectAsync().ConfigureAwait(false);
            _dispatcher.TryEnqueue(() =>
            {
                Projects.Clear();
                foreach (var project in projects) Projects.Add(project);
                ActiveProject = active;
                ActiveProjectChanged?.Invoke(active);
            });
        }
        catch (Exception ex)
        {
            App.Log($"projects refresh failed: {ex.GetType().Name}: {ex.Message}");
        }
    }

    public async Task<Project> CreateAsync(string name, string? description, string? instructions, string? workspacePath, List<string>? localFolders)
    {
        var project = await _api.CreateProjectAsync(new CreateProjectRequest
        {
            Name = name,
            Description = description,
            Instructions = instructions,
            WorkspacePath = workspacePath,
            LocalFolders = localFolders,
        }).ConfigureAwait(false);

        _dispatcher.TryEnqueue(() => Projects.Add(project));
        return project;
    }

    [RelayCommand]
    public async Task ActivateAsync(Project project)
    {
        var updated = await _api.ActivateProjectAsync(project.Id).ConfigureAwait(false);
        _dispatcher.TryEnqueue(() =>
        {
            ActiveProject = updated;
            ActiveProjectChanged?.Invoke(updated);
        });
    }

    [RelayCommand]
    public async Task DeleteAsync(Project project)
    {
        var ok = await _api.DeleteProjectAsync(project.Id).ConfigureAwait(false);
        if (ok)
        {
            _dispatcher.TryEnqueue(() => Projects.Remove(project));
        }
    }
}
