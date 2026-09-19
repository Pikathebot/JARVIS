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

        var show = new Microsoft.UI.Xaml.Controls.MenuFlyoutItem { Text = "Show / Hide Jarvis" };
        show.Click += (_, _) => ShowRequested?.Invoke();
        menu.Items.Add(show);

        var hud = new Microsoft.UI.Xaml.Controls.MenuFlyoutItem { Text = "Toggle HUD" };
        hud.Click += (_, _) => ToggleHudRequested?.Invoke();
        menu.Items.Add(hud);

        var freeVram = new Microsoft.UI.Xaml.Controls.MenuFlyoutItem { Text = "Free VRAM" };
        freeVram.Click += (_, _) => FreeVramRequested?.Invoke();
        menu.Items.Add(freeVram);

        menu.Items.Add(new Microsoft.UI.Xaml.Controls.MenuFlyoutSeparator());

        var exit = new Microsoft.UI.Xaml.Controls.MenuFlyoutItem { Text = "Exit Jarvis" };
        exit.Click += (_, _) => ExitRequested?.Invoke();
        menu.Items.Add(exit);

        _icon.ContextFlyout = menu;
        _icon.LeftClickCommand = new RelayShowCommand(() => ShowRequested?.Invoke());
        _icon.ForceCreate();
    }

    public void Dispose() => _icon.Dispose();

    private sealed class RelayShowCommand : System.Windows.Input.ICommand
    {
        private readonly Action _action;
        public RelayShowCommand(Action action) => _action = action;
#pragma warning disable CS0067 // required by ICommand; this command's enabled state never changes
        public event EventHandler? CanExecuteChanged;
#pragma warning restore CS0067
        public bool CanExecute(object? parameter) => true;
        public void Execute(object? parameter) => _action();
    }
}
