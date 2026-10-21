using Jarvis.Core.Models;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;

namespace Jarvis_App;

/// <summary>
/// Loading the chat model ahead of a message (user, 2026-10-21: "so i wont need to send a message
/// everytime to load the model"). The first keystroke in the composer, or a mic tap, asks the
/// backend to start loading in the background (POST /models/warm), so the model is mostly ready by
/// the time the message goes; the status popover's Load model button does the same on demand. The
/// backend never waits or overrides: Game mode, voice mode or a busy PC hold it, and the message
/// itself loads as before.
/// </summary>
public sealed partial class MainWindow
{
    /// <summary>At most one warm-up request this often while typing (each is a cheap health check
    /// once the model is up).</summary>
    private static readonly TimeSpan WarmEvery = TimeSpan.FromSeconds(20);
    private DateTime _warmAt = DateTime.MinValue;

    private void WarmModelSoon()
    {
        if (DateTime.UtcNow - _warmAt < WarmEvery) return;
        _warmAt = DateTime.UtcNow;
        _ = WarmModelAsync();
    }

    private async Task<WarmModelResponse?> WarmModelAsync()
    {
        try { return await _api.WarmModelAsync(); }
        catch { return null; } // offline: the governor poll says so
    }

    private async void LoadModel_Click(object sender, RoutedEventArgs e)
    {
        _warmAt = DateTime.UtcNow;
        LoadModelButton.Text = "Loading…";
        var result = await WarmModelAsync();
        // The pill shows the load itself (the governor reports Loading); the button says what
        // happened to this press, then goes back to its label.
        LoadModelButton.Text = result?.State switch
        {
            "ready" => "Loaded",
            "held" => "Not now",
            null => "Offline",
            _ => "Loading…",
        };
        ToolTipService.SetToolTip(LoadModelButton, result?.State == "held" ? result.Reason : null);
        await Task.Delay(TimeSpan.FromSeconds(result?.State == "loading" ? 8 : 3));
        LoadModelButton.Text = "Load model";
    }
}
