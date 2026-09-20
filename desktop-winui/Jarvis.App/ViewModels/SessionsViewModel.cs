using System.Collections.ObjectModel;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using Jarvis.Core.Api;
using Jarvis.Core.Models;
using Microsoft.UI.Dispatching;

namespace Jarvis_App.ViewModels;

/// <summary>
/// Port of the session-list half of Sidebar.tsx: GET /sessions (optionally scoped to the active
/// project), per-session delete, and select-to-load. The "default" session id is undeletable and
/// relabelled "Default Workspace" — same rule as the TS sidebar.
/// </summary>
public partial class SessionsViewModel : ObservableObject
{
    private const string DefaultSessionId = "default";

    private readonly JarvisApiClient _api;
    private readonly DispatcherQueue _dispatcher;

    public ObservableCollection<Session> Sessions { get; } = new();

    [ObservableProperty]
    public partial string? ProjectId { get; set; }

    public event Func<string, Task>? SessionSelected;

    public SessionsViewModel(JarvisApiClient api, DispatcherQueue dispatcher)
    {
        _api = api;
        _dispatcher = dispatcher;
    }

    [RelayCommand]
    public async Task RefreshAsync()
    {
        try
        {
            var sessions = await _api.FetchSessionsAsync(ProjectId).ConfigureAwait(false);
            _dispatcher.TryEnqueue(() =>
            {
                Sessions.Clear();
                foreach (var session in sessions.OrderByDescending(s => s.UpdatedAt))
                {
                    Sessions.Add(session);
                }
            });
        }
        catch (Exception ex)
        {
            // sidebar just stays with whatever it last had
            App.Log($"sessions refresh failed: {ex.GetType().Name}: {ex.Message}");
        }
    }

    public static string DisplayLabel(Session session) =>
        session.SessionId == DefaultSessionId
            ? "Default Workspace"
            : session.LastMessage is { Length: > 0 } msg
                ? (msg.Length > 40 ? msg[..40] + "…" : msg)
                : $"Session {session.SessionId[..Math.Min(12, session.SessionId.Length)]}…";

    public static bool IsDeletable(Session session) => session.SessionId != DefaultSessionId;

    [RelayCommand]
    public async Task SelectAsync(Session session)
    {
        if (SessionSelected is not null)
        {
            await SessionSelected(session.SessionId).ConfigureAwait(false);
        }
    }

    [RelayCommand]
    public async Task DeleteAsync(Session session)
    {
        if (!IsDeletable(session)) return;
        var ok = await _api.DeleteSessionAsync(session.SessionId).ConfigureAwait(false);
        if (ok)
        {
            _dispatcher.TryEnqueue(() => Sessions.Remove(session));
        }
    }
}
