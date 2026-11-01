using Jarvis.Core.Models;
using Jarvis_App.Themes;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;

namespace Jarvis_App.Views;

/// <summary>
/// Settings → Updates (backend/app/updates): llama.cpp and the voice packages, each with what is
/// installed, what is newest and Update / Roll back buttons. Nothing is fetched from GitHub or
/// PyPI until "Check for updates"; a running job is polled once a second while the page shows.
/// </summary>
public sealed partial class SettingsPane
{
    private DispatcherTimer? _updatePoll;
    private string? _watchedJob;

    private async Task RefreshUpdatesAsync(bool check)
    {
        UpdatesCheckButton.IsEnabled = false;
        if (check) ShowUpdatesStatus("Asking GitHub and PyPI…");
        try
        {
            var status = await _api.FetchUpdatesAsync(check);
            ShowUpdatesStatus(null);
            UpdatesCheckedText.Text = status.CheckedAt is { } at ? $"Checked {Ago(at)}" : "Not checked yet";
            ShowTools(status.Tools, busy: status.Job?.Status == "running");
            ShowJob(status.Job);
        }
        catch (Exception ex)
        {
            ShowUpdatesStatus($"Can't reach the backend ({ex.Message}).");
        }
        finally
        {
            UpdatesCheckButton.IsEnabled = true;
        }
    }

    private void ShowUpdatesStatus(string? text)
    {
        UpdatesStatusText.Text = text ?? "";
        UpdatesStatusText.Visibility = string.IsNullOrEmpty(text) ? Visibility.Collapsed : Visibility.Visible;
    }

    private async void UpdatesCheck_Click(object sender, RoutedEventArgs e) => await RefreshUpdatesAsync(check: true);

    private void ShowTools(List<ToolUpdate> tools, bool busy)
    {
        UpdatesList.Children.Clear();
        foreach (var tool in tools)
        {
            var section = new StackPanel { Spacing = 6 };
            section.Children.Add(new TextBlock
            {
                Text = tool.Name, Style = AppStyle("SectionHeaderText"), Margin = new Thickness(4, 0, 0, 0),
            });
            var rows = new StackPanel();
            if (tool.Kind == "llama")
            {
                var version = tool.Installed is null ? "—"
                    : tool.UpdateAvailable ? $"{tool.Installed} → {tool.Latest}" : $"{tool.Installed}" + (tool.Latest is null ? "" : " · up to date");
                rows.Children.Add(VersionRow(tool.Detail, null, version));
            }
            else
            {
                rows.Children.Add(VersionRow(tool.Detail, null, ""));
                foreach (var package in tool.Packages)
                {
                    rows.Children.Add(new Microsoft.UI.Xaml.Shapes.Rectangle { Style = (Style)Resources["RowSeparator"] });
                    var version = package.Installed ?? "not installed";
                    if (package.Outdated) version += $" → {package.Latest}";
                    else if (package.Latest is not null) version += " · up to date";
                    rows.Children.Add(VersionRow(package.Name, package.Held ? "Held: a CUDA build, kept as it is" : null, version));
                }
            }
            rows.Children.Add(new Microsoft.UI.Xaml.Shapes.Rectangle { Style = (Style)Resources["RowSeparator"] });
            rows.Children.Add(ActionsRow(tool, busy));
            section.Children.Add(new Border { Style = (Style)Resources["SettingsCard"], Child = rows });
            UpdatesList.Children.Add(section);
        }
    }

    private Grid VersionRow(string label, string? caption, string version)
    {
        var row = new Grid { Style = (Style)Resources["SettingsRow"], ColumnSpacing = 12 };
        row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        var text = new StackPanel { VerticalAlignment = VerticalAlignment.Center };
        text.Children.Add(new TextBlock { Text = label, Style = AppStyle("BodyText"), TextWrapping = TextWrapping.Wrap });
        if (caption is not null) text.Children.Add(new TextBlock { Text = caption, Style = AppStyle("SecondaryFootnoteText") });
        var value = new TextBlock { Text = version, Style = AppStyle("DataText"), VerticalAlignment = VerticalAlignment.Center };
        Grid.SetColumn(value, 1);
        row.Children.Add(text);
        row.Children.Add(value);
        return row;
    }

    /// <summary>What happened last (or is queued, or went wrong) on the left; the buttons on the right.</summary>
    private Grid ActionsRow(ToolUpdate tool, bool busy)
    {
        var row = new Grid { Style = (Style)Resources["SettingsRow"], ColumnSpacing = 12 };
        row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });

        var (line, kind) = tool.Error is not null ? (tool.Error, StatusKind.Error)
            : tool.Pending is not null ? (tool.Pending, StatusKind.Warning)
            : tool.Last is { } last ? ((last.Ok ? "" : "Failed: ") + last.Message, last.Ok ? StatusKind.Success : StatusKind.Error)
            : tool.UpdateAvailable ? ("An update is available.", StatusKind.Info)
            : ("Nothing to do.", StatusKind.Neutral);
        var status = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 6, VerticalAlignment = VerticalAlignment.Center };
        var brush = new Microsoft.UI.Xaml.Media.SolidColorBrush(StatusStyle.ColorOf(kind));
        status.Children.Add(new FontIcon
        {
            Style = AppStyle("JarvisIcon"), Glyph = StatusStyle.GlyphOf(kind, inProgress: tool.Pending is not null),
            FontSize = 12, Foreground = brush, VerticalAlignment = VerticalAlignment.Top, Margin = new Thickness(0, 3, 0, 0),
        });
        status.Children.Add(new TextBlock
        {
            Text = line, Style = AppStyle("SecondaryFootnoteText"), TextWrapping = TextWrapping.Wrap, MaxWidth = 380,
        });
        row.Children.Add(status);

        var buttons = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8, VerticalAlignment = VerticalAlignment.Center };
        if (tool.Pending is not null)
        {
            buttons.Children.Add(ToolButton("Cancel", !busy, async () => await _api.CancelPendingUpdateAsync(tool.Id)));
        }
        else
        {
            if (tool.RollBackTo is not null)
                buttons.Children.Add(ToolButton($"Roll back", !busy, () => StartUpdateAsync(tool.Id, "rollback"),
                    tip: $"Back to {tool.RollBackTo}"));
            var size = tool.DownloadSize is { } bytes ? $" ({SizeFormat.Bytes(bytes)})" : "";
            buttons.Children.Add(ToolButton($"Update{size}", !busy && tool.UpdateAvailable, () => StartUpdateAsync(tool.Id, "update")));
        }
        Grid.SetColumn(buttons, 1);
        row.Children.Add(buttons);
        return row;
    }

    private Button ToolButton(string label, bool enabled, Func<Task> act, string? tip = null)
    {
        var button = new Button { Content = label, IsEnabled = enabled };
        if (tip is not null) ToolTipService.SetToolTip(button, tip);
        button.Click += async (_, _) =>
        {
            button.IsEnabled = false;
            try
            {
                await act();
            }
            catch (Exception ex)
            {
                ShowUpdatesStatus(ex.Message);
            }
            await RefreshUpdatesAsync(check: false);
        };
        return button;
    }

    private async Task StartUpdateAsync(string toolId, string action)
    {
        var response = await _api.StartUpdateAsync(toolId, action);
        ShowJob(response.Job);
    }

    // --- the running job -----------------------------------------------------------------------

    private void ShowJob(UpdateJob? job)
    {
        var running = job?.Status == "running";
        // A finished job stays on screen only as its tool's "last" line.
        UpdateJobPanel.Visibility = running ? Visibility.Visible : Visibility.Collapsed;
        if (job is null) return;
        if (running)
        {
            var tool = job.Tool == "llama-cpp" ? "llama.cpp" : "voice packages";
            UpdateJobText.Text = $"{(job.Action == "rollback" ? "Rolling back" : "Updating")} {tool}: {job.Stage}";
            UpdateJobProgress.IsIndeterminate = job.BytesTotal <= 0 || job.Stage != "Downloading";
            UpdateJobProgress.Value = job.BytesTotal > 0 ? 100.0 * job.BytesDone / job.BytesTotal : 0;
            UpdateJobDetail.Text = job.Stage == "Downloading" && job.BytesTotal > 0
                ? $"{SizeFormat.Bytes(job.BytesDone)} of {SizeFormat.Bytes(job.BytesTotal)}" : "";
            UpdateJobCancel.IsEnabled = job.Stage == "Downloading";
            _watchedJob = job.Id;
            StartUpdatePolling();
        }
        else if (_watchedJob == job.Id)
        {
            _watchedJob = null;
            StopUpdatePolling();
            _ = RefreshUpdatesAsync(check: false); // the tools' versions and "last" lines moved
        }
    }

    private void StartUpdatePolling()
    {
        if (_updatePoll is null)
        {
            _updatePoll = new DispatcherTimer { Interval = TimeSpan.FromSeconds(1) };
            _updatePoll.Tick += async (_, _) =>
            {
                if (_page != "Updates") { StopUpdatePolling(); return; }
                try
                {
                    ShowJob((await _api.FetchUpdateJobAsync()).Job);
                }
                catch
                {
                    // the backend is busy or restarting: the next tick tries again
                }
            };
        }
        if (!_updatePoll.IsEnabled) _updatePoll.Start();
    }

    private void StopUpdatePolling() => _updatePoll?.Stop();

    private async void UpdateJobCancel_Click(object sender, RoutedEventArgs e)
    {
        UpdateJobCancel.IsEnabled = false;
        try
        {
            await _api.CancelUpdateDownloadAsync();
        }
        catch (Exception ex)
        {
            ShowUpdatesStatus(ex.Message);
        }
    }

    private static Style AppStyle(string key) => (Style)Application.Current.Resources[key];
}
