using System.Collections.ObjectModel;
using Jarvis_App.ViewModels;
using Jarvis_Glass;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;

namespace Jarvis_App.Views;

/// <summary>Create-a-workspace sheet, opened from the workspace dropdown. Collects the fields of
/// <c>POST /api/projects</c> (name, description, instructions, the location -- the folder the
/// workspace is -- and extra folders to index), creates through
/// <see cref="ProjectsViewModel.CreateAsync"/> and switches to the new workspace. An in-window
/// glass sheet; the host removes it on <see cref="CloseRequested"/>.</summary>
public sealed partial class NewWorkspacePane : UserControl
{
    public event Action? CloseRequested;

    /// <summary>The sheet itself, apart from its scrim: what rises on open (MainWindow.OpenSheet).</summary>
    public UIElement SheetSurface => Sheet;

    public ObservableCollection<string> Folders { get; } = new();

    private readonly ProjectsViewModel _projects;
    private readonly nint _hwnd;
    private bool _busy;

    /// <summary>The picked workspace folder, or null for a new folder of Jarvis's own.</summary>
    private string? _location;

    public NewWorkspacePane(ProjectsViewModel projects, nint hwnd)
    {
        _projects = projects;
        _hwnd = hwnd;
        InitializeComponent();
        // Not in XAML: a bare integer there is what let the sheet sit on the same
        // layer as the controls behind it, which it could neither cover nor frost.
        Sheet.Layer = GlassLayers.Sheet;
        Loaded += (_, _) => NameBox.Focus(FocusState.Programmatic);
    }

    private void Close_Click(object sender, RoutedEventArgs e) => CloseRequested?.Invoke();

    private void Scrim_Tapped(object sender, TappedRoutedEventArgs e) => CloseRequested?.Invoke();

    /// <summary>Taps inside the sheet must not reach the scrim's close handler.</summary>
    private void Sheet_Tapped(object sender, TappedRoutedEventArgs e) => e.Handled = true;

    private void NameBox_TextChanged(object sender, TextChangedEventArgs e) =>
        CreateButton.IsEnabled = !_busy && NameBox.Text.Trim().Length > 0;

    private void NameBox_KeyDown(object sender, KeyRoutedEventArgs e)
    {
        if (e.Key == Windows.System.VirtualKey.Enter && CreateButton.IsEnabled)
        {
            e.Handled = true;
            Create_Click(sender, e);
        }
    }

    /// <summary>The Explorer folder dialog; null when cancelled. A failure shows in the sheet
    /// instead of vanishing (WinRT's FolderPicker used to fail here with no trace).</summary>
    private string? PickFolder(string title)
    {
        try
        {
            ErrorText.Visibility = Visibility.Collapsed;
            return Services.FolderDialog.Pick(_hwnd, title,
                _location ?? Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments));
        }
        catch (Exception ex)
        {
            App.Log($"folder dialog failed: {ex.GetType().Name}: {ex.Message}");
            ErrorText.Text = $"Could not open the folder picker: {ex.Message}";
            ErrorText.Visibility = Visibility.Visible;
            return null;
        }
    }

    private void ChooseLocation_Click(object sender, RoutedEventArgs e)
    {
        var path = PickFolder("Choose the workspace folder");
        if (path is null) return;
        _location = path;
        LocationText.Text = path;
        ClearLocationButton.Visibility = Visibility.Visible;
        // A workspace made from a folder is usually named after it.
        if (NameBox.Text.Trim().Length == 0) NameBox.Text = Path.GetFileName(path.TrimEnd('\\', '/'));
    }

    private void ClearLocation_Click(object sender, RoutedEventArgs e)
    {
        _location = null;
        LocationText.Text = "New folder in Jarvis's workspaces";
        ClearLocationButton.Visibility = Visibility.Collapsed;
    }

    private void AddFolder_Click(object sender, RoutedEventArgs e)
    {
        var path = PickFolder("Add a folder to index");
        if (path is null) return;
        if (!Folders.Contains(path, StringComparer.OrdinalIgnoreCase)) Folders.Add(path);
    }

    private void RemoveFolder_Click(object sender, RoutedEventArgs e)
    {
        if (sender is FrameworkElement { Tag: string path }) Folders.Remove(path);
    }

    private async void Create_Click(object sender, RoutedEventArgs e)
    {
        var name = NameBox.Text.Trim();
        if (_busy || name.Length == 0) return;

        _busy = true;
        CreateButton.IsEnabled = false;
        ErrorText.Visibility = Visibility.Collapsed;
        try
        {
            var project = await _projects.CreateAsync(
                name,
                NullIfBlank(DescriptionBox.Text),
                NullIfBlank(InstructionsBox.Text),
                workspacePath: _location,
                Folders.Count > 0 ? Folders.ToList() : null);
            // A new workspace is almost always the one you want to be in; the backend only
            // activates the very first project on its own.
            await _projects.ActivateAsync(project);
            CloseRequested?.Invoke();
        }
        catch (Exception ex)
        {
            App.Log($"workspace create failed: {ex.GetType().Name}: {ex.Message}");
            ErrorText.Text = $"Could not create the workspace: {ex.Message}";
            ErrorText.Visibility = Visibility.Visible;
            _busy = false;
            CreateButton.IsEnabled = true;
        }
    }

    private static string? NullIfBlank(string text)
    {
        var trimmed = text.Trim();
        return trimmed.Length == 0 ? null : trimmed;
    }
}
