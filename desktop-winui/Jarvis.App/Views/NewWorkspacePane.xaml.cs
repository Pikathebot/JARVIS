using System.Collections.ObjectModel;
using Jarvis_App.ViewModels;
using Jarvis_Glass;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;

namespace Jarvis_App.Views;

/// <summary>Create-a-workspace sheet, opened from the workspace dropdown. Collects the fields of
/// <c>POST /api/projects</c> (name, description, instructions, local folders to index), creates
/// through <see cref="ProjectsViewModel.CreateAsync"/> and switches to the new workspace. Same
/// in-window-sheet shape as <see cref="SettingsPane"/>; the host removes it on
/// <see cref="CloseRequested"/>.</summary>
public sealed partial class NewWorkspacePane : UserControl
{
    public event Action? CloseRequested;

    public ObservableCollection<string> Folders { get; } = new();

    private readonly ProjectsViewModel _projects;
    private readonly nint _hwnd;
    private bool _busy;

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

    private async void AddFolder_Click(object sender, RoutedEventArgs e)
    {
        var picker = new Windows.Storage.Pickers.FolderPicker();
        WinRT.Interop.InitializeWithWindow.Initialize(picker, _hwnd);
        picker.FileTypeFilter.Add("*");
        picker.SuggestedStartLocation = Windows.Storage.Pickers.PickerLocationId.DocumentsLibrary;

        var folder = await picker.PickSingleFolderAsync();
        if (folder is null || string.IsNullOrEmpty(folder.Path)) return;
        if (!Folders.Contains(folder.Path, StringComparer.OrdinalIgnoreCase)) Folders.Add(folder.Path);
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
                workspacePath: null,
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
