using H.NotifyIcon;

namespace Jarvis_App.Services;

/// <summary>The tray icon: a click shows Jarvis; a right-click asks for the tray popup
/// (<see cref="TrayPopupWindow"/>), which replaced the native menu on 2026-10-20.</summary>
public sealed class TrayService : IDisposable
{
    private readonly TaskbarIcon _icon;

    public event Action? ShowRequested;
    public event Action? PopupRequested;

    public TrayService(string iconResourcePath)
    {
        _icon = new TaskbarIcon
        {
            ToolTipText = "Jarvis Assistant (Active)",
            IconSource = new Microsoft.UI.Xaml.Media.Imaging.BitmapImage(new Uri(iconResourcePath)),
        };

        // The popup is its own window, not a ContextFlyout: H.NotifyIcon turns a flyout into a
        // native menu built once, so its labels could never follow the state.
        _icon.RightClickCommand = new RelayCommand(() => PopupRequested?.Invoke());
        // Single AND double click both show the window. A double-click goes only to
        // DoubleClickCommand -- with just a LeftClickCommand the usual double-click on a tray
        // icon did nothing at all. NoLeftClickDelay: a single click shows at once instead of
        // waiting out the double-click interval first.
        var showCommand = new RelayCommand(() => ShowRequested?.Invoke());
        _icon.LeftClickCommand = showCommand;
        _icon.DoubleClickCommand = showCommand;
        _icon.NoLeftClickDelay = true;
        // false: the default (true) also puts the whole process into Windows Efficiency Mode,
        // meant for tray-only apps -- not one running a renderer and supervising the backend.
        _icon.ForceCreate(false);
    }

    public void SetGameMode(bool on) =>
        _icon.ToolTipText = on ? "Jarvis Assistant (Game mode: no model loaded)" : "Jarvis Assistant (Active)";

    public void Dispose() => _icon.Dispose();

    private sealed class RelayCommand : System.Windows.Input.ICommand
    {
        private readonly Action _action;
        public RelayCommand(Action action) => _action = action;
#pragma warning disable CS0067 // required by ICommand; this command's enabled state never changes
        public event EventHandler? CanExecuteChanged;
#pragma warning restore CS0067
        public bool CanExecute(object? parameter) => true;
        public void Execute(object? parameter) => _action();
    }
}
