using H.NotifyIcon;

namespace Jarvis_App.Services;

/// <summary>Replaces the retired pywebview launcher's pystray icon: Show/Hide, Toggle HUD, Free VRAM, Exit.</summary>
public sealed class TrayService : IDisposable
{
    private readonly TaskbarIcon _icon;

    public event Action? ShowRequested;
    public event Action? ToggleHudRequested;
    public event Action? FreeVramRequested;
    public event Action? ExitRequested;

    public TrayService(string iconResourcePath)
    {
        _icon = new TaskbarIcon
        {
            ToolTipText = "Jarvis Assistant (Active)",
            IconSource = new Microsoft.UI.Xaml.Media.Imaging.BitmapImage(new Uri(iconResourcePath)),
        };

        var menu = new Microsoft.UI.Xaml.Controls.MenuFlyout();

        // Menu items carry a Command, not a Click handler: in the default PopupMenu mode the
        // flyout is rebuilt as a native menu and only each item's Command is carried over.
        menu.Items.Add(new Microsoft.UI.Xaml.Controls.MenuFlyoutItem { Text = "Show Jarvis", Command = new RelayCommand(() => ShowRequested?.Invoke()) });
        menu.Items.Add(new Microsoft.UI.Xaml.Controls.MenuFlyoutItem { Text = "Toggle HUD", Command = new RelayCommand(() => ToggleHudRequested?.Invoke()) });
        menu.Items.Add(new Microsoft.UI.Xaml.Controls.MenuFlyoutItem { Text = "Free VRAM", Command = new RelayCommand(() => FreeVramRequested?.Invoke()) });
        menu.Items.Add(new Microsoft.UI.Xaml.Controls.MenuFlyoutSeparator());
        menu.Items.Add(new Microsoft.UI.Xaml.Controls.MenuFlyoutItem { Text = "Exit Jarvis", Command = new RelayCommand(() => ExitRequested?.Invoke()) });

        _icon.ContextFlyout = menu;
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
