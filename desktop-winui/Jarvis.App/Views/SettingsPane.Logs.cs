using System.Text;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Input;

namespace Jarvis_App.Views;

/// <summary>
/// Settings -> Developer -> Logs (PLAN 4.17). Backend, Model and Sidecars come from
/// GET /api/logs (filtered there; the 2 s status polls are left out); App is this process's own
/// logs, read from disk here. Follow re-reads every 2 s while the page shows and keeps the view
/// at the bottom unless the reader has scrolled up.
/// </summary>
public sealed partial class SettingsPane
{
    private static readonly string[] LogSources = { "backend", "model", "sidecars", "app" };
    private const int LogLines = 300;
    private const int AppLogLinesPerFile = 120;

    private DispatcherTimer? _logTimer;
    private bool _logBusy;
    private string? _logFolder;

    private string CurrentLogSource => LogSources[Math.Clamp(LogSourceSwitch.SelectedIndex, 0, LogSources.Length - 1)];

    private void StartLogs()
    {
        _ = RefreshLogsAsync(scrollToEnd: true);
        if (LogFollowToggle.IsOn) StartLogTimer();
    }

    private void StopLogs() => _logTimer?.Stop();

    private void StartLogTimer()
    {
        if (_logTimer is null)
        {
            _logTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(2) };
            _logTimer.Tick += (_, _) => _ = RefreshLogsAsync(scrollToEnd: false);
        }
        _logTimer.Start();
    }

    private async Task RefreshLogsAsync(bool scrollToEnd)
    {
        if (_logBusy) return;
        _logBusy = true;
        var atEnd = scrollToEnd || LogScroll.VerticalOffset >= LogScroll.ScrollableHeight - 4;
        var source = CurrentLogSource;
        var warnings = LogWarningsToggle.IsOn;
        var search = LogSearchBox.Text;
        try
        {
            IReadOnlyList<string> lines;
            string status;
            if (source == "app")
            {
                lines = await Task.Run(() => ReadAppLogs(search, warnings));
                _logFolder = AppContext.BaseDirectory;
                status = $"{lines.Count(l => !l.StartsWith("── "))} lines from the app's own logs";
            }
            else
            {
                var response = await _api.FetchLogsAsync(source, LogLines, warnings, search);
                lines = response.Lines;
                _logFolder = response.Path is { } path ? Path.GetDirectoryName(path) : App.RepoRoot;
                status = response.TotalMatched > response.Lines.Count
                    ? $"Last {response.Lines.Count} of {response.TotalMatched:N0} lines"
                    : $"{response.Lines.Count} lines";
            }
            if (source != CurrentLogSource) return; // the switch moved while this was in flight

            var text = lines.Count == 0 ? EmptyLogText(source, warnings, search) : string.Join("\n", lines);
            if (text != LogText.Text)
            {
                LogText.Text = text;
                if (atEnd)
                {
                    LogScroll.UpdateLayout();
                    LogScroll.ChangeView(null, LogScroll.ScrollableHeight, null, disableAnimation: true);
                }
            }
            LogStatus.Text = $"{status} · {DateTime.Now:HH:mm:ss}";
        }
        catch (Exception ex)
        {
            LogStatus.Text = $"Couldn't read the logs: {ex.Message}";
        }
        finally
        {
            _logBusy = false;
        }
    }

    private static string EmptyLogText(string source, bool warnings, string search) =>
        warnings || !string.IsNullOrWhiteSpace(search) ? "No lines match the filter."
        : source switch
        {
            "model" => "Nothing yet: the model hasn't printed anything since the backend started.",
            "sidecars" => "Nothing yet: no sidecar (captioner, embeddings, Laya) has run since the backend started.",
            "app" => "None of the app's log files exist yet.",
            _ => "backend.log is empty or missing.",
        };

    /// <summary>The tail of each of the app's own log files, newest last, each under a header.</summary>
    private static List<string> ReadAppLogs(string search, bool warningsOnly)
    {
        var files = new List<string>();
        if (App.RepoRoot is { } root) files.Add(Path.Combine(root, "launcher-winui.log"));
        foreach (var name in new[] { "jarvis-app.log", "jarvis-voice.log", "jarvis-glass.log", "jarvis-app-crash.log" })
            files.Add(Path.Combine(AppContext.BaseDirectory, name));

        var needle = search?.Trim() ?? "";
        var output = new List<string>();
        foreach (var file in files.Where(File.Exists))
        {
            var kept = TailLines(file)
                .Where(l => needle.Length == 0 || l.Contains(needle, StringComparison.OrdinalIgnoreCase))
                .Where(l => !warningsOnly || LooksLikeTrouble(l))
                .TakeLast(AppLogLinesPerFile)
                .ToList();
            if (kept.Count == 0) continue;
            output.Add($"── {Path.GetFileName(file)} ({File.GetLastWriteTime(file):yyyy-MM-dd HH:mm}) ──");
            output.AddRange(kept);
        }
        return output;
    }

    private static bool LooksLikeTrouble(string line) =>
        line.Contains("error", StringComparison.OrdinalIgnoreCase) || line.Contains("exception", StringComparison.OrdinalIgnoreCase)
        || line.Contains("fail", StringComparison.OrdinalIgnoreCase) || line.Contains("warn", StringComparison.OrdinalIgnoreCase);

    /// <summary>The last 256 KB as lines (a partial first line dropped). The file stays open for
    /// writing by whoever owns it.</summary>
    private static List<string> TailLines(string path)
    {
        try
        {
            using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
            var start = Math.Max(0, stream.Length - 256 * 1024);
            stream.Seek(start, SeekOrigin.Begin);
            using var reader = new StreamReader(stream, Encoding.UTF8);
            var lines = reader.ReadToEnd().Split('\n').Select(l => l.TrimEnd('\r')).Where(l => l.Length > 0).ToList();
            if (start > 0 && lines.Count > 0) lines.RemoveAt(0);
            return lines;
        }
        catch (IOException)
        {
            return new List<string>();
        }
        catch (UnauthorizedAccessException)
        {
            return new List<string>();
        }
    }

    // --- controls ------------------------------------------------------------------------------

    private void LogSource_SelectionChanged(object sender, RoutedEventArgs e)
    {
        if (_api is null || _page != "Developer") return;
        LogText.Text = "";
        _ = RefreshLogsAsync(scrollToEnd: true);
    }

    private void LogWarningsToggle_Toggled(object sender, RoutedEventArgs e)
    {
        if (_api is null || _page != "Developer") return;
        _ = RefreshLogsAsync(scrollToEnd: true);
    }

    private void LogFollowToggle_Toggled(object sender, RoutedEventArgs e)
    {
        if (_api is null || _page != "Developer") return; // IsOn="True" in XAML fires this inside InitializeComponent
        if (LogFollowToggle.IsOn) StartLogTimer();
        else StopLogs();
    }

    private void LogSearchBox_KeyDown(object sender, KeyRoutedEventArgs e)
    {
        if (e.Key != Windows.System.VirtualKey.Enter) return;
        e.Handled = true;
        _ = RefreshLogsAsync(scrollToEnd: true);
    }

    private void LogRefresh_Click(object sender, RoutedEventArgs e) => _ = RefreshLogsAsync(scrollToEnd: true);

    private void LogCopy_Click(object sender, RoutedEventArgs e)
    {
        var package = new Windows.ApplicationModel.DataTransfer.DataPackage();
        package.SetText(LogText.Text ?? "");
        Windows.ApplicationModel.DataTransfer.Clipboard.SetContent(package);
        LogStatus.Text = "Copied what's shown.";
    }

    private void LogOpenFolder_Click(object sender, RoutedEventArgs e)
    {
        var folder = _logFolder ?? App.RepoRoot;
        if (string.IsNullOrEmpty(folder) || !Directory.Exists(folder)) return;
        try
        {
            System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo("explorer.exe", $"\"{folder}\"") { UseShellExecute = true });
        }
        catch (Exception ex)
        {
            LogStatus.Text = $"Couldn't open the folder: {ex.Message}";
        }
    }
}
