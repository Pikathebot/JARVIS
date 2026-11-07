using Jarvis.Core.Api;
using Jarvis.Core.Models;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;

namespace Jarvis_App.Views;

/// <summary>A workspace's own page (design system: ProjectPage). Slice 1 of PLAN 4.18 phase 4: the
/// header (name, folder, Reindex) and the editable Instructions. The host calls <see cref="Show"/>
/// with the project to display; the page never switches projects itself.</summary>
public sealed partial class ProjectPage : UserControl
{
    private JarvisApiClient? _api;
    private Project? _project;
    private bool _busy;

    public ProjectPage()
    {
        InitializeComponent();
    }

    private ProjectState? _state;
    private bool _stateBusy;

    public void Initialize(JarvisApiClient api) => _api = api;

    /// <summary>Set by the host: opens a chat from the Chats list, and starts a new one.</summary>
    public Func<Session, Task>? OpenSession { get; set; }
    public Action? NewChat { get; set; }

    private const int FileRowsShown = 8;
    private const int ChatRowsShown = 10;
    private static readonly TimeSpan UndoWindow = TimeSpan.FromSeconds(8);

    // ---- Memories, Files, Chats --------------------------------------------------------------

    private static FrameworkElement Note(string text) => new TextBlock
    {
        Text = text,
        Style = Themes.JarvisTheme.Style("SecondaryFootnoteText"),
        TextWrapping = TextWrapping.Wrap,
        Margin = new Thickness(16, 14, 16, 14),
    };

    /// <summary>A list row: a hairline above every row but the first, 44 px tall at least.</summary>
    private static Border RowShell(UIElement content, bool first, double minHeight = 44) => new()
    {
        Child = content,
        MinHeight = minHeight,
        Padding = new Thickness(14, 0, 14, 0),
        BorderThickness = new Thickness(0, first ? 0 : 1, 0, 0),
        BorderBrush = Themes.JarvisTheme.Brush("Separator"),
    };

    private static TextBlock Label(string text, string style, string? brush = null)
    {
        var block = new TextBlock { Text = text, Style = Themes.JarvisTheme.Style(style), VerticalAlignment = VerticalAlignment.Center };
        if (brush is not null) block.Foreground = Themes.JarvisTheme.Brush(brush);
        return block;
    }

    private static FontIcon Icon(string glyph, string brush, double size = 16) => new()
    {
        Glyph = glyph,
        FontSize = size,
        Style = Themes.JarvisTheme.Style("JarvisIcon"),
        Foreground = Themes.JarvisTheme.Brush(brush),
        VerticalAlignment = VerticalAlignment.Center,
    };

    private async Task LoadMemoriesAsync(Project project)
    {
        MemoriesList.Children.Clear();
        MemoriesList.Children.Add(Note("Loading…"));
        try
        {
            // The list call also returns general memories (no project); this page shows the project's own.
            var all = await _api!.FetchMemoriesAsync(projectId: project.Id, limit: 100);
            if (_project?.Id != project.Id) return;
            ShowMemories(all.Where(m => m.ProjectId == project.Id).ToList());
        }
        catch (Exception ex)
        {
            App.Log($"loading project memories failed: {ex.GetType().Name}: {ex.Message}");
            if (_project?.Id != project.Id) return;
            MemoriesList.Children.Clear();
            MemoriesList.Children.Add(Note($"Could not load: {ex.Message}"));
        }
    }

    private void ShowMemories(List<MemoryItem> memories)
    {
        MemoriesList.Children.Clear();
        if (memories.Count == 0)
        {
            MemoriesList.Children.Add(Note("Nothing remembered for this project yet."));
            return;
        }
        for (var i = 0; i < memories.Count; i++) MemoriesList.Children.Add(MemoryRow(memories[i], i == 0));
    }

    private Border MemoryRow(MemoryItem memory, bool first)
    {
        var grid = new Grid { ColumnSpacing = 12, MinHeight = 44 };
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });

        var fact = Label(memory.Content, "BodyText");
        fact.TextWrapping = TextWrapping.Wrap;
        fact.MaxLines = 2;
        fact.TextTrimming = TextTrimming.CharacterEllipsis;
        fact.Margin = new Thickness(0, 8, 0, 8);
        var kind = Label(KindName(memory.Category), "SecondaryFootnoteText");
        var remove = new Button
        {
            Content = Icon("", "LabelTertiary", 14),
            Background = new Microsoft.UI.Xaml.Media.SolidColorBrush(Microsoft.UI.Colors.Transparent),
            BorderThickness = new Thickness(0),
            Padding = new Thickness(6),
            VerticalAlignment = VerticalAlignment.Center,
        };
        ToolTipService.SetToolTip(remove, "Remove (kept in the bin for 30 days)");
        Grid.SetColumn(fact, 1);
        Grid.SetColumn(kind, 2);
        Grid.SetColumn(remove, 3);
        grid.Children.Add(Icon("", "LabelSecondary"));
        grid.Children.Add(fact);
        grid.Children.Add(kind);
        grid.Children.Add(remove);

        var shell = RowShell(grid, first);
        remove.Click += async (_, _) => await RemoveMemoryAsync(memory, shell);
        return shell;
    }

    /// <summary>"preference" -> "Preference"; "task_context" -> "Task context".</summary>
    private static string KindName(string category)
    {
        var text = category.Replace('_', ' ').Trim();
        return text.Length == 0 ? "" : char.ToUpperInvariant(text[0]) + text[1..];
    }

    /// <summary>Removing sends the memory to the bin; the row turns into an Undo line for a few seconds.</summary>
    private async Task RemoveMemoryAsync(MemoryItem memory, Border shell)
    {
        if (_api is null) return;
        bool ok;
        try
        {
            ok = await _api.UndoRememberAsync(memory.Id);
        }
        catch (Exception ex)
        {
            App.Log($"removing a project memory failed: {ex.GetType().Name}: {ex.Message}");
            ok = false;
        }
        if (!ok) return;

        var line = new Grid { ColumnSpacing = 12, MinHeight = 44 };
        line.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        line.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        var text = Label($"Removed (kept in the bin): {memory.Content}", "SecondaryFootnoteText");
        text.TextWrapping = TextWrapping.Wrap;
        text.MaxLines = 2;
        text.TextTrimming = TextTrimming.CharacterEllipsis;
        var undo = new HyperlinkButton { Content = "Undo", Padding = new Thickness(4, 0, 4, 0) };
        Grid.SetColumn(undo, 1);
        line.Children.Add(text);
        line.Children.Add(undo);
        shell.Child = line;

        var undone = false;
        undo.Click += async (_, _) =>
        {
            undone = true;
            try { await _api.RestoreForgottenMemoryAsync(memory.Id); }
            catch (Exception ex) { App.Log($"restoring a project memory failed: {ex.GetType().Name}: {ex.Message}"); }
            if (_project is not null) await LoadMemoriesAsync(_project);
        };
        await Task.Delay(UndoWindow);
        if (!undone && _project is not null) await LoadMemoriesAsync(_project);
    }

    private async Task LoadFilesAsync(Project project)
    {
        FilesList.Children.Clear();
        FilesList.Children.Add(Note("Loading…"));
        try
        {
            var files = await _api!.FetchProjectFilesAsync(project.Id);
            if (_project?.Id != project.Id) return;
            ShowFiles(files, all: false);
        }
        catch (Exception ex)
        {
            App.Log($"loading project files failed: {ex.GetType().Name}: {ex.Message}");
            if (_project?.Id != project.Id) return;
            FilesList.Children.Clear();
            FilesList.Children.Add(Note($"Could not load: {ex.Message}"));
        }
    }

    private void ShowFiles(List<ProjectFile> files, bool all)
    {
        FilesList.Children.Clear();
        if (files.Count == 0)
        {
            FilesList.Children.Add(Note("No files yet."));
            return;
        }
        var shown = all ? files : files.Take(FileRowsShown).ToList();
        for (var i = 0; i < shown.Count; i++)
        {
            var file = shown[i];
            var grid = new Grid { ColumnSpacing = 12 };
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            var name = Label(file.Name, "BodyText");
            name.TextTrimming = TextTrimming.CharacterEllipsis;
            Grid.SetColumn(name, 1);
            grid.Children.Add(Icon(file.IsDir ? "" : "", "LabelSecondary"));
            grid.Children.Add(name);
            if (!file.IsDir)
            {
                var size = Label(FileSize(file.SizeBytes), "DataText", "LabelSecondary");
                Grid.SetColumn(size, 2);
                grid.Children.Add(size);
            }
            FilesList.Children.Add(RowShell(grid, i == 0));
        }
        if (!all && files.Count > shown.Count)
        {
            var more = new HyperlinkButton { Content = $"Show all {files.Count}", Padding = new Thickness(0) };
            more.Click += (_, _) => ShowFiles(files, all: true);
            FilesList.Children.Add(RowShell(more, first: false));
        }
    }

    private static string FileSize(long bytes) => bytes switch
    {
        >= 1L << 20 => $"{bytes / (double)(1L << 20):0.#} MB",
        >= 1L << 10 => $"{bytes / (double)(1L << 10):0.#} KB",
        _ => $"{bytes} B",
    };

    private async Task LoadChatsAsync(Project project)
    {
        ChatsList.Children.Clear();
        ChatsList.Children.Add(Note("Loading…"));
        try
        {
            var sessions = await _api!.FetchSessionsAsync(project.Id, "WORKSPACE");
            if (_project?.Id != project.Id) return;
            ShowChats(sessions, all: false);
        }
        catch (Exception ex)
        {
            App.Log($"loading project chats failed: {ex.GetType().Name}: {ex.Message}");
            if (_project?.Id != project.Id) return;
            ChatsList.Children.Clear();
            ChatsList.Children.Add(Note($"Could not load: {ex.Message}"));
        }
    }

    private void ShowChats(List<Session> sessions, bool all)
    {
        ChatsList.Children.Clear();
        if (sessions.Count == 0)
        {
            ChatsList.Children.Add(Note("No chats yet. Start one."));
            return;
        }
        var ordered = sessions.OrderByDescending(s => s.UpdatedAt ?? 0).ToList();
        var shown = all ? ordered : ordered.Take(ChatRowsShown).ToList();
        for (var i = 0; i < shown.Count; i++)
        {
            var session = shown[i];
            var title = string.IsNullOrWhiteSpace(session.Title) ? "New chat" : session.Title!;
            var grid = new Grid { ColumnSpacing = 12 };
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            var name = Label(title, "BodyText");
            name.TextTrimming = TextTrimming.CharacterEllipsis;
            var when = Label(ChatDate(session.UpdatedAt), "SecondaryFootnoteText");
            Grid.SetColumn(when, 1);
            grid.Children.Add(name);
            grid.Children.Add(when);
            var button = new Button
            {
                Content = grid,
                HorizontalAlignment = HorizontalAlignment.Stretch,
                HorizontalContentAlignment = HorizontalAlignment.Stretch,
                Background = new Microsoft.UI.Xaml.Media.SolidColorBrush(Microsoft.UI.Colors.Transparent),
                BorderThickness = new Thickness(0),
                Padding = new Thickness(0),
                MinHeight = 34,
            };
            button.Click += async (_, _) =>
            {
                if (OpenSession is not null) await OpenSession(session);
            };
            ChatsList.Children.Add(RowShell(button, i == 0, minHeight: 34));
        }
        if (!all && ordered.Count > shown.Count)
        {
            var more = new HyperlinkButton { Content = $"Show all {ordered.Count}", Padding = new Thickness(0) };
            more.Click += (_, _) => ShowChats(sessions, all: true);
            ChatsList.Children.Add(RowShell(more, first: false));
        }
    }

    /// <summary>Unix seconds -> "Today", "Yesterday", "5 Nov" (with the year when it is not this one).</summary>
    private static string ChatDate(double? unixSeconds)
    {
        if (unixSeconds is null) return "";
        var day = DateTimeOffset.FromUnixTimeSeconds((long)unixSeconds.Value).LocalDateTime.Date;
        var today = DateTime.Today;
        if (day == today) return "Today";
        if (day == today.AddDays(-1)) return "Yesterday";
        return day.Year == today.Year ? day.ToString("d MMM") : day.ToString("d MMM yyyy");
    }

    private void NewChat_Click(object sender, RoutedEventArgs e) => NewChat?.Invoke();

    // ---- Project state (.jarvis/PROJECT.md) ------------------------------------------------

    private async Task LoadStateAsync(Project project)
    {
        _state = null;
        EditStateLink.Visibility = Visibility.Collapsed;
        StateView.Visibility = Visibility.Visible;
        StateBox.Visibility = Visibility.Collapsed;
        StateButtons.Visibility = Visibility.Collapsed;
        StateView.Content = new TextBlock { Text = "Loading…", Foreground = Themes.JarvisTheme.Brush("LabelSecondary") };
        try
        {
            var state = await _api!.GetProjectStateAsync(project.Id);
            if (_project?.Id != project.Id) return; // the user moved to another workspace meanwhile
            _state = state;
            StateNote.Text = $"Jarvis keeps this current as work lands. Stored in {state.Path}, so you can edit it in any editor too. A change reaches Jarvis from your next chat.";
            RenderState();
            EditStateLink.Visibility = Visibility.Visible;
        }
        catch (Exception ex)
        {
            App.Log($"loading project state failed: {ex.GetType().Name}: {ex.Message}");
            if (_project?.Id != project.Id) return;
            StateView.Content = new TextBlock { Text = $"Could not load: {ex.Message}", TextWrapping = TextWrapping.Wrap, Foreground = Themes.JarvisTheme.Brush("StatusError") };
        }
    }

    /// <summary>The Markdown renderer has no task-list items, so the checkboxes become ballot-box glyphs.</summary>
    private void RenderState()
    {
        var text = (_state?.Content ?? "").Replace("- [ ] ", "- ☐ ").Replace("- [x] ", "- ☑ ").Replace("- [X] ", "- ☑ ");
        StateView.Content = Rendering.MarkdownRenderer.Render(text, isStreaming: false);
        StateView.Visibility = Visibility.Visible;
        StateBox.Visibility = Visibility.Collapsed;
        StateButtons.Visibility = Visibility.Collapsed;
        EditStateLink.Visibility = Visibility.Visible;
    }

    private void EditState_Click(object sender, RoutedEventArgs e)
    {
        if (_state is null) return;
        StateBox.Text = _state.Content;
        StateView.Visibility = Visibility.Collapsed;
        StateBox.Visibility = Visibility.Visible;
        StateButtons.Visibility = Visibility.Visible;
        EditStateLink.Visibility = Visibility.Collapsed;
        UpdateStateCount();
        StateBox.Focus(FocusState.Programmatic);
    }

    private void CancelState_Click(object sender, RoutedEventArgs e) => RenderState();

    /// <summary>The text box joins lines with a bare CR; the file and the cap count LF.</summary>
    private string StateText() => StateBox.Text.Replace("\r\n", "\n").Replace('\r', '\n');

    private void StateBox_TextChanged(object sender, TextChangedEventArgs e) => UpdateStateCount();

    private void UpdateStateCount()
    {
        if (_state is null) return;
        var length = StateText().Length;
        var max = _state.MaxChars;
        StateCount.Text = length > max
            ? $"{length:N0} / {max:N0} characters. Too long to save: trim {length - max:N0}."
            : $"{length:N0} / {max:N0} characters";
        StateCount.Foreground = Themes.JarvisTheme.Brush(length > max ? "StatusError" : length >= max * 0.9 ? "StatusWarning" : "LabelSecondary");
        SaveStateButton.IsEnabled = !_stateBusy && length <= max;
    }

    private async void SaveState_Click(object sender, RoutedEventArgs e)
    {
        if (_api is null || _project is null || _state is null || _stateBusy) return;
        _stateBusy = true;
        SaveStateButton.IsEnabled = false;
        try
        {
            _state = await _api.PutProjectStateAsync(_project.Id, StateText());
            RenderState();
        }
        catch (Exception ex)
        {
            App.Log($"saving project state failed: {ex.GetType().Name}: {ex.Message}");
            StateCount.Text = $"Could not save: {ex.Message}";
            StateCount.Foreground = Themes.JarvisTheme.Brush("StatusError");
        }
        finally
        {
            _stateBusy = false;
            UpdateStateCount();
        }
    }

    /// <summary>Loads <paramref name="project"/> into the page and leaves any edit mode.</summary>
    public void Show(Project project)
    {
        _project = project;
        NameText.Text = project.Name;
        var kept = IsKeptByJarvis(project.WorkspacePath);
        FolderText.Text = kept ? "Kept by Jarvis" : project.WorkspacePath;
        OpenFolderLink.Visibility = kept || string.IsNullOrWhiteSpace(project.WorkspacePath) ? Visibility.Collapsed : Visibility.Visible;
        IndexText.Text = "Press Reindex after files change outside Jarvis.";
        ReindexButton.IsEnabled = true;
        ShowInstructions();
        _ = LoadStateAsync(project);
        _ = LoadMemoriesAsync(project);
        _ = LoadFilesAsync(project);
        _ = LoadChatsAsync(project);
    }

    /// <summary>A project with no folder of its own lives under Jarvis's workspace/projects directory.</summary>
    private static bool IsKeptByJarvis(string? path) =>
        string.IsNullOrWhiteSpace(path)
        || path.Replace('/', '\\').Contains(@"\workspace\projects\", StringComparison.OrdinalIgnoreCase);

    private void ShowInstructions()
    {
        var text = _project?.Instructions?.Trim() ?? "";
        var empty = text.Length == 0;
        InstructionsText.Text = empty ? "No instructions. Add rules Jarvis should follow in every chat here." : text;
        InstructionsText.Foreground = (Microsoft.UI.Xaml.Media.Brush)Application.Current.Resources[empty ? "LabelSecondaryBrush" : "LabelBrush"];
        InstructionsText.Visibility = Visibility.Visible;
        InstructionsBox.Visibility = Visibility.Collapsed;
        InstructionsButtons.Visibility = Visibility.Collapsed;
        EditInstructionsLink.Visibility = Visibility.Visible;
    }

    private void EditInstructions_Click(object sender, RoutedEventArgs e)
    {
        InstructionsBox.Text = _project?.Instructions ?? "";
        InstructionsText.Visibility = Visibility.Collapsed;
        InstructionsBox.Visibility = Visibility.Visible;
        InstructionsButtons.Visibility = Visibility.Visible;
        EditInstructionsLink.Visibility = Visibility.Collapsed;
        InstructionsBox.Focus(FocusState.Programmatic);
    }

    private void CancelInstructions_Click(object sender, RoutedEventArgs e) => ShowInstructions();

    /// <summary>Enter saves, Shift+Enter adds a line (the composer's habit).</summary>
    private void InstructionsBox_KeyDown(object sender, Microsoft.UI.Xaml.Input.KeyRoutedEventArgs e)
    {
        if (e.Key != Windows.System.VirtualKey.Enter) return;
        var shift = Microsoft.UI.Input.InputKeyboardSource.GetKeyStateForCurrentThread(Windows.System.VirtualKey.Shift)
            .HasFlag(Windows.UI.Core.CoreVirtualKeyStates.Down);
        if (shift) return;
        e.Handled = true;
        SaveInstructions_Click(sender, e);
    }

    private async void SaveInstructions_Click(object sender, RoutedEventArgs e)
    {
        if (_api is null || _project is null || _busy) return;
        _busy = true;
        SaveInstructionsButton.IsEnabled = false;
        try
        {
            var updated = await _api.UpdateProjectAsync(_project.Id, new UpdateProjectRequest { Instructions = InstructionsBox.Text });
            _project.Instructions = updated.Instructions;
            ShowInstructions();
        }
        catch (Exception ex)
        {
            App.Log($"saving project instructions failed: {ex.GetType().Name}: {ex.Message}");
            InstructionsNote.Text = $"Could not save: {ex.Message}";
        }
        finally
        {
            _busy = false;
            SaveInstructionsButton.IsEnabled = true;
        }
    }

    private async void Reindex_Click(object sender, RoutedEventArgs e)
    {
        if (_api is null || _project is null) return;
        ReindexButton.IsEnabled = false;
        IndexText.Text = "Indexing…";
        try
        {
            var stats = await _api.ReindexProjectAsync(_project.Id);
            // The backend skips files whose hash is unchanged, so "indexed" counts only the ones re-read.
            var files = stats.ScannedFiles == 1 ? "1 file" : $"{stats.ScannedFiles} files";
            IndexText.Text = stats.IndexedFiles == 0
                ? $"Up to date · {files} checked · {DateTime.Now:HH:mm}"
                : $"{stats.IndexedFiles} updated · {files} checked · {DateTime.Now:HH:mm}";
        }
        catch (Exception ex)
        {
            App.Log($"reindex failed: {ex.GetType().Name}: {ex.Message}");
            IndexText.Text = $"Index failed: {ex.Message}";
        }
        finally
        {
            ReindexButton.IsEnabled = true;
        }
    }

    private async void OpenFolder_Click(object sender, RoutedEventArgs e)
    {
        var path = _project?.WorkspacePath;
        if (string.IsNullOrWhiteSpace(path)) return;
        try
        {
            await Windows.System.Launcher.LaunchFolderPathAsync(path);
        }
        catch (Exception ex)
        {
            App.Log($"opening project folder failed: {ex.GetType().Name}: {ex.Message}");
        }
    }
}
