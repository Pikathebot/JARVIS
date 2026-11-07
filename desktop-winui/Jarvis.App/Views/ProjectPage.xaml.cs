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
