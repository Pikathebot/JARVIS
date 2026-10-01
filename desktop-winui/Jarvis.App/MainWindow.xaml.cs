using Jarvis.Core.Api;
using Jarvis.Core.Models;
using Jarvis.Core.Sse;
using Jarvis_App.Services;
using Jarvis_App.ViewModels;
using Jarvis_App.Views;
using Microsoft.UI;
using Microsoft.UI.Input;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Automation;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using Windows.System;
using Windows.UI;

namespace Jarvis_App;

public sealed partial class MainWindow : Window
{
    public ChatViewModel ChatViewModel { get; }
    public GovernorViewModel GovernorViewModel { get; }
    public AwarenessViewModel AwarenessViewModel { get; }
    public SessionsViewModel SessionsViewModel { get; }
    public ProjectsViewModel ProjectsViewModel { get; }
    public RightPanelViewModel RightPanelViewModel { get; }
    public PersonaViewModel PersonaViewModel { get; }
    public RoutinesViewModel RoutinesViewModel { get; }
    public VoiceViewModel VoiceViewModel { get; }
    public ModelsViewModel ModelsViewModel { get; }

    private readonly JarvisApiClient _api;
    private readonly GlobalHotkeyService _hotkey;
    private readonly AppWindow _appWindow;

    /// <summary>True while something can bring the window back (the tray icon); the X button then
    /// hides instead of closing. Cleared before a real exit.</summary>
    public bool HideOnClose { get; set; }

    /// <summary>Shows the window again after X hid it, restores it if minimized, and brings it to
    /// the front. Used by the tray icon and by a second launch of the app.</summary>
    public void ShowFromBackground()
    {
        _appWindow.Show();
        if (_appWindow.Presenter is OverlappedPresenter { State: OverlappedPresenterState.Minimized } presenter)
        {
            presenter.Restore();
        }
        Activate();
    }
    public HudWindow? Hud { get; set; }

    /// <summary>
    /// Drives every glass panel's rendering tier from live telemetry. Owned here rather than in
    /// App because it needs the governor and awareness view-models, which this window creates.
    /// </summary>
    /// <summary>The liquid-glass renderer behind this window (see GlassHost).</summary>
    public Jarvis_Glass.GlassHost Glass { get; }

    public MainWindow(JarvisApiClient api, ChatStreamClient streamClient, AwarenessStreamClient awarenessStream)
    {
        InitializeComponent();
        // Layers are named rather than written as integers in XAML -- see GlassLayers for
        // why the sheets' hand-written "2" left them unable to cover the window behind them.
        WorkspaceDropdown.Layer = Jarvis_Glass.GlassLayers.Popover;
        StatusPopover.Layer = Jarvis_Glass.GlassLayers.Popover;
        // Panels hidden, startup card up, before anything publishes glass (MainWindow.Motion.cs).
        BeginStartup();

        _api = api;
        var dispatcher = Microsoft.UI.Dispatching.DispatcherQueue.GetForCurrentThread();

        ChatViewModel = new ChatViewModel(api, streamClient, dispatcher);
        GovernorViewModel = new GovernorViewModel(api, dispatcher);
        AwarenessViewModel = new AwarenessViewModel(api, awarenessStream, dispatcher);
        SessionsViewModel = new SessionsViewModel(api, dispatcher);
        ProjectsViewModel = new ProjectsViewModel(api, dispatcher);
        RightPanelViewModel = new RightPanelViewModel(api, dispatcher);
        PersonaViewModel = new PersonaViewModel(api, dispatcher);
        RoutinesViewModel = new RoutinesViewModel(api, dispatcher);
        VoiceViewModel = new VoiceViewModel(api, dispatcher, "jarvis-main");
        ModelsViewModel = new ModelsViewModel(api, dispatcher);


        GovernorViewModel.PropertyChanged += (_, _) => UpdateGovernorPill();
        // The popover names the fast model; the catalogue says which that is.
        ModelsViewModel.PropertyChanged += (_, _) => UpdateGovernorPill();
        GovernorViewModel.BackendCameOnline += () => _ = ModelsViewModel.RefreshAsync();
        GovernorViewModel.Start();
        StartTelemetry();
        AwarenessViewModel.Start();

        ChatViewModel.PropertyChanged += (_, args) =>
        {
            if (args.PropertyName == nameof(ChatViewModel.Error))
            {
                DispatcherQueue.TryEnqueue(UpdateErrorBanner);
            }
            else if (args.PropertyName == nameof(ChatViewModel.LatestRetrieval))
            {
                DispatcherQueue.TryEnqueue(() =>
                {
                    UpdateContextTab();
                    if (ChatViewModel.LatestRetrieval?.ChunksUsed.Count > 0) AutoOpenInspector(ContextTab);
                });
            }
            else if (args.PropertyName == nameof(ChatViewModel.ActiveReasoningMessage))
            {
                DispatcherQueue.TryEnqueue(RebindReasoningMessage);
            }
            else if (args.PropertyName == nameof(ChatViewModel.IsReasoningStreaming))
            {
                DispatcherQueue.TryEnqueue(UpdateReasoningBadge);
            }
        };

        AwarenessTrayHost.Content = new Views.AwarenessTray(AwarenessViewModel);
        ChatViewModel.ActivitySteps.CollectionChanged += (_, _) => DispatcherQueue.TryEnqueue(RefreshActivityList);

        VoiceViewModel.PropertyChanged += (_, args) =>
        {
            if (args.PropertyName == nameof(ViewModels.VoiceViewModel.State) ||
                args.PropertyName == nameof(ViewModels.VoiceViewModel.ErrorMessage))
            {
                DispatcherQueue.TryEnqueue(UpdateVoiceStateText);
            }
        };
        VoiceViewModel.CommandReceived += query => DispatcherQueue.TryEnqueue(async () => await HandleVoiceCommandAsync(query));
        ChatViewModel.MessageCompleted += finished =>
        {
            if (VoiceViewModel.IsActive)
            {
                var toSpeak = finished.Spoken ?? finished.Content;
                if (!string.IsNullOrWhiteSpace(toSpeak))
                {
                    _ = VoiceViewModel.SpeakAsync(toSpeak);
                }
            }
        };
        ChatViewModel.SpokenFeedback += text =>
        {
            if (VoiceViewModel.IsActive)
            {
                _ = VoiceViewModel.SpeakAsync(text);
            }
        };

        RightPanelViewModel.PropertyChanged += (_, args) =>
        {
            if (args.PropertyName == nameof(ViewModels.RightPanelViewModel.IsOpen))
            {
                DispatcherQueue.TryEnqueue(() =>
                {
                    var open = RightPanelViewModel.IsOpen;
                    RightPanel.Visibility = open ? Visibility.Visible : Visibility.Collapsed;
                    PanelIcon.Foreground = Themes.JarvisTheme.Brush(open ? "Accent" : "Label");
                    ApplyResponsiveLayout(RootGrid.ActualWidth); // column vs overlay depends on width
                });
            }
        };

        SessionsViewModel.SessionSelected += async sessionId =>
        {
            CloseSidebarOverlay();
            await ChatViewModel.LoadSessionAsync(sessionId);
        };
        ProjectsViewModel.ActiveProjectChanged += project =>
        {
            ChatViewModel.ProjectId = project?.Id;
            SessionsViewModel.ProjectId = project?.Id;
            UpdateSpaceChrome();
            UpdateWorkspaceHighlight();
            _sessionsRefresh = SessionsViewModel.RefreshAsync();
        };
        // Space and ephemeral state can change from inside the view model too (opening a stored
        // session leaves ephemeral mode), so the chrome follows the properties, not the clicks.
        ChatViewModel.PropertyChanged += (_, args) =>
        {
            if (args.PropertyName is nameof(ViewModels.ChatViewModel.Space) or nameof(ViewModels.ChatViewModel.IsEphemeral))
            {
                DispatcherQueue.TryEnqueue(UpdateSpaceChrome);
            }
        };
        // Reopen in the space it was left in. Ephemeral is deliberately not restored: a
        // scratchpad never outlives the run it was started in.
        if (LoadSavedSpace() == ChatSpace.Freeform)
        {
            SessionsViewModel.Space = ChatSpace.Freeform;
            _ = ChatViewModel.SwitchSpaceAsync(ChatSpace.Freeform);
        }
        RestoreModelChoice();
        ChatViewModel.MessageCompleted += completedMessage => DispatcherQueue.TryEnqueue(() => _ = SessionsViewModel.RefreshAsync());
        // A turn that wrote an artifact brings the inspector up on it (once per chat).
        ChatViewModel.MessageCompleted += completedMessage => DispatcherQueue.TryEnqueue(() =>
        {
            if (completedMessage.ToolSteps.Any(s => s.Status == ToolStatus.Success
                    && s.Tool.Contains("artifact", StringComparison.OrdinalIgnoreCase)))
            {
                AutoOpenInspector(ArtifactsTab);
            }
        });
        SessionsViewModel.Sessions.CollectionChanged += (_, _) => DispatcherQueue.TryEnqueue(RebuildSessionGroups);
        ChatViewModel.PropertyChanged += (_, args) =>
        {
            if (args.PropertyName == nameof(ViewModels.ChatViewModel.ActiveSessionId)) DispatcherQueue.TryEnqueue(UpdateSessionHighlight);
        };

        // Initial state is fetched when the backend is reachable, not now: the window is built
        // before BackendHost has uvicorn up, so a fetch here fails silently and nothing would
        // retry it -- the sidebar stayed empty and the workspace picker stuck on its placeholder
        // until the first completed turn. Projects first; ActiveProjectChanged scopes and
        // refreshes the session list. The first time, the window then opens onto them. Also
        // re-runs after a backend restart.
        GovernorViewModel.BackendCameOnline += OnBackendCameOnline;
        // The session list virtualises and recycles its rows now, and Loaded does not fire
        // again for a recycled container, so the active tint is applied per content change too.
        SessionsList.ContainerContentChanging += (_, args) =>
        {
            if (!args.InRecycleQueue && args.Item is Session session
                && args.ItemContainer.ContentTemplateRoot is Grid row)
            {
                ApplySessionHighlight(row, session);
            }
        };

        RootGrid.AddHandler(UIElement.PointerPressedEvent, new Microsoft.UI.Xaml.Input.PointerEventHandler(RootGrid_PointerPressedForDropdown), handledEventsToo: true);
        ExtendTitleBar();
        // Liquid glass under this window's XAML tree.
        Glass = new Jarvis_Glass.GlassHost(this);

        _hotkey = new GlobalHotkeyService();
        var hwnd = WinRT.Interop.WindowNative.GetWindowHandle(this);
        _hotkey.Register(hwnd);
        _hotkey.HotkeyPressed += () => DispatcherQueue.TryEnqueue(() => Hud?.ToggleVisible());

        // X hides to the tray rather than closing. A closed WinUI window is gone for good, but the
        // process lived on (the HUD and the tray icon keep it running), so the tray's Show and a
        // second launch -- which single-instance redirects here -- both found nothing to show.
        _appWindow = AppWindow.GetFromWindowId(Win32Interop.GetWindowIdFromWindow(hwnd));
        _appWindow.Closing += (_, e) =>
        {
            if (!HideOnClose) return;
            e.Cancel = true;
            _appWindow.Hide();
        };

        Closed += (_, _) =>
        {
            VoiceViewModel.Dispose();
            _hotkey.Dispose();
            GovernorViewModel.Dispose();
            AwarenessViewModel.Dispose();
        };
    }

    private void ExtendTitleBar()
    {
        var hwnd = WinRT.Interop.WindowNative.GetWindowHandle(this);
        var windowId = Win32Interop.GetWindowIdFromWindow(hwnd);
        var appWindow = AppWindow.GetFromWindowId(windowId);

        if (AppWindowTitleBar.IsCustomizationSupported())
        {
            appWindow.TitleBar.ExtendsContentIntoTitleBar = true;
            appWindow.TitleBar.PreferredHeightOption = TitleBarHeightOption.Tall;
            appWindow.TitleBar.ButtonBackgroundColor = Colors.Transparent;
            appWindow.TitleBar.ButtonInactiveBackgroundColor = Colors.Transparent;
        }

        appWindow.Resize(new Windows.Graphics.SizeInt32(1280, 800));
        // Below this the composer and one message column stop being usable; the layout adapts
        // down to here and the window refuses to go smaller.
        if (appWindow.Presenter is OverlappedPresenter presenter)
        {
            presenter.PreferredMinimumWidth = 420;
            presenter.PreferredMinimumHeight = 480;
        }

        RootGrid.SizeChanged += (_, args) => ApplyResponsiveLayout(args.NewSize.Width);
        // The caption row's controls move with the layout (sidebar hidden, inspector docked,
        // the status text shed), and each move changes which strips of the top edge drag.
        TitleBarRow.LayoutUpdated += (_, _) => UpdateTitleBarRegions();
    }

    private Windows.Graphics.RectInt32[] _passthrough = Array.Empty<Windows.Graphics.RectInt32>();

    /// <summary>
    /// The top 48 px drag the window, except where a control sits: the toolbar's capsules and
    /// the sidebar's compose button live in the caption row, and a drag region swallows every
    /// click. Non-client regions are in physical pixels.
    /// </summary>
    private void UpdateTitleBarRegions()
    {
        if (RootGrid.XamlRoot is null || !AppWindowTitleBar.IsCustomizationSupported()) return;
        var scale = RootGrid.XamlRoot.RasterizationScale;
        var rects = new List<Windows.Graphics.RectInt32>();
        foreach (var element in new FrameworkElement[] { SidebarCapsule, StatusCapsule, ToolsCapsule, ComposeButton })
        {
            if (element.Visibility != Visibility.Visible || element.ActualWidth <= 0) continue;
            var b = element.TransformToVisual(RootGrid).TransformBounds(new Windows.Foundation.Rect(0, 0, element.ActualWidth, element.ActualHeight));
            rects.Add(new Windows.Graphics.RectInt32(
                (int)Math.Round(b.X * scale), (int)Math.Round(b.Y * scale),
                (int)Math.Round(b.Width * scale), (int)Math.Round(b.Height * scale)));
        }
        if (rects.SequenceEqual(_passthrough)) return; // LayoutUpdated fires for any layout pass
        _passthrough = rects.ToArray();

        var source = InputNonClientPointerSource.GetForWindowId(_appWindow?.Id ?? Win32Interop.GetWindowIdFromWindow(WinRT.Interop.WindowNative.GetWindowHandle(this)));
        source.SetRegionRects(NonClientRegionKind.Caption, new[]
        {
            new Windows.Graphics.RectInt32(0, 0, (int)Math.Round(RootGrid.ActualWidth * scale), (int)Math.Round(48 * scale)),
        });
        source.SetRegionRects(NonClientRegionKind.Passthrough, _passthrough);
    }

    // ------------------------------------------------------------------ responsive layout
    //
    // Rule: the sidebar and the inspector push the chat (take their own column) whenever the
    // chat keeps at least MinChatWidth; only when it would not do they leave the grid and float
    // over the chat. The toolbar's sidebar button hides a docked sidebar, or opens the floating
    // one. The glass slabs need real layout changes (they publish their bounds to the scene on
    // LayoutUpdated), so this is code, not visual states.
    private const double MinChatWidth = 360;
    private const double SidebarWidth = 260;
    private const double SidebarGap = 8;
    private const double PanelWidth = 380;
    private const double PanelGap = 8;
    /// <summary>Room the system caption buttons take at the window's top right.</summary>
    private const double CaptionButtonsWidth = 138;
    /// <summary>The caption row (toolbar) plus its gap: where the floating panels start.</summary>
    private const double ToolbarHeight = 48;
    private bool _compactSidebar;
    private bool _sidebarOverlayOpen;
    private bool _sidebarHidden;

    private void ApplyResponsiveLayout(double width)
    {
        var available = width - BodyGrid.Padding.Left - BodyGrid.Padding.Right;
        var panelOpen = RightPanelViewModel.IsOpen;

        // Sidebar docks if the chat still fits beside it, unless hidden from the toolbar.
        var sidebarDocked = !_sidebarHidden && available - (SidebarWidth + SidebarGap) >= MinChatWidth;
        var compact = !sidebarDocked;
        if (compact != _compactSidebar)
        {
            _compactSidebar = compact;
            _sidebarOverlayOpen = false;
            if (compact)
            {
                // Out of the column, floating over the chat below the toolbar.
                SidebarColumn.Width = new GridLength(0);
                Grid.SetColumn(SidebarSlab, 1);
                SidebarSlab.HorizontalAlignment = HorizontalAlignment.Left;
                SidebarSlab.Width = SidebarWidth;
                SidebarSlab.Margin = new Thickness(0, ToolbarHeight, 0, 0);
                Canvas.SetZIndex(SidebarSlab, 20);
                SidebarSlab.Layer = 3; // above the chat slab it now overlaps, like the dropdown
                SidebarSlab.Visibility = Visibility.Collapsed;
            }
            else
            {
                SidebarColumn.Width = new GridLength(SidebarWidth);
                Grid.SetColumn(SidebarSlab, 0);
                SidebarSlab.HorizontalAlignment = HorizontalAlignment.Stretch;
                SidebarSlab.Width = double.NaN;
                SidebarSlab.Margin = new Thickness(0, 0, SidebarGap, 0);
                Canvas.SetZIndex(SidebarSlab, 0);
                SidebarSlab.ClearValue(Jarvis_Glass.GlassSlab.LayerProperty);
                SidebarSlab.Visibility = Visibility.Visible;
            }
        }

        // The inspector docks if the chat still fits beside it and the (docked) sidebar.
        var usedBySidebar = sidebarDocked ? SidebarWidth + SidebarGap : 0;
        var panelDocked = available - usedBySidebar - (PanelWidth + PanelGap) >= MinChatWidth;
        if (panelDocked)
        {
            RightPanelColumn.Width = new GridLength(panelOpen ? PanelWidth : 0);
            Grid.SetColumn(RightPanel, 2);
            RightPanel.HorizontalAlignment = HorizontalAlignment.Stretch;
            RightPanel.Width = double.NaN;
            RightPanel.Margin = new Thickness(PanelGap, ToolbarHeight, 0, 0);
            Canvas.SetZIndex(RightPanel, 0);
            RightPanel.ClearValue(Jarvis_Glass.GlassSlab.LayerProperty);
        }
        else
        {
            RightPanelColumn.Width = new GridLength(0);
            Grid.SetColumn(RightPanel, 1);
            RightPanel.HorizontalAlignment = HorizontalAlignment.Right;
            RightPanel.Width = Math.Min(PanelWidth, Math.Max(240, available - 24));
            // Floating, it starts below the toolbar so the button that closes it stays reachable.
            RightPanel.Margin = new Thickness(0, ToolbarHeight, 0, 0);
            Canvas.SetZIndex(RightPanel, 10);
            RightPanel.Layer = 3;
        }

        // The caption buttons sit over whatever column is rightmost: the toolbar keeps clear of
        // them unless a docked inspector is there to take them.
        TitleBarRow.Margin = new Thickness(0, 0, panelDocked && panelOpen ? 0 : CaptionButtonsWidth, 0);

        // A narrow toolbar sheds the VRAM figure first (the popover still has it).
        var chatWidth = available - usedBySidebar - (panelDocked && panelOpen ? PanelWidth + PanelGap : 0);
        StatusVramText.Visibility = chatWidth >= 560 ? Visibility.Visible : Visibility.Collapsed;
        SetStatusPopoverOpen(false);
    }

    private void SidebarToggle_Click(object sender, RoutedEventArgs e)
    {
        var dockable = RootGrid.ActualWidth - BodyGrid.Padding.Left - BodyGrid.Padding.Right - (SidebarWidth + SidebarGap) >= MinChatWidth;
        if (!_compactSidebar || (_sidebarHidden && dockable))
        {
            // Wide enough to dock: the button hides and restores the docked sidebar.
            _sidebarHidden = !_sidebarHidden;
            ApplyResponsiveLayout(RootGrid.ActualWidth);
            return;
        }
        _sidebarOverlayOpen = !_sidebarOverlayOpen;
        SidebarSlab.Visibility = _sidebarOverlayOpen ? Visibility.Visible : Visibility.Collapsed;
    }

    /// <summary>In compact mode the floating sidebar closes once a session is picked.</summary>
    private void CloseSidebarOverlay()
    {
        if (_compactSidebar && _sidebarOverlayOpen)
        {
            _sidebarOverlayOpen = false;
            SidebarSlab.Visibility = Visibility.Collapsed;
        }
    }

    // ------------------------------------------------------------------ status pill + popover

    /// <summary>The model's short name ("Qwen3.5 9B") from its GGUF path: the file name up to
    /// the first quant/precision token, dashes as spaces.</summary>
    internal static string ShortModelName(string? path)
    {
        if (string.IsNullOrWhiteSpace(path)) return "";
        var name = System.IO.Path.GetFileNameWithoutExtension(path.Replace('\\', '/'));
        var kept = new List<string>();
        foreach (var part in name.Split('-', '_'))
        {
            if (System.Text.RegularExpressions.Regex.IsMatch(part, @"^(UD|I?Q\d.*|BF16|F16|F32|GGUF)$", System.Text.RegularExpressions.RegexOptions.IgnoreCase)) break;
            kept.Add(part);
        }
        return kept.Count > 0 ? string.Join(' ', kept) : name;
    }

    private void UpdateGovernorPill()
    {
        var (word, kind) = GovernorViewModel.Status switch
        {
            "ok" => ("Online", Themes.StatusKind.Success),
            "throttled" => ("Throttled", Themes.StatusKind.Warning),
            "degraded" => ("Degraded", Themes.StatusKind.Warning),
            _ => ("Offline", Themes.StatusKind.Error),
        };
        var brush = new Microsoft.UI.Xaml.Media.SolidColorBrush(Themes.StatusStyle.ColorOf(kind));
        var model = ShortModelName(GovernorViewModel.ConfiguredModel);

        // Healthy: the pill names the model (the green tick is the word "online"). Anything
        // else: the status word itself, in its colour, so the change reads without the icon.
        GovernorPillIcon.Glyph = Themes.StatusStyle.GlyphOf(kind);
        GovernorPillIcon.Foreground = brush;
        if (kind == Themes.StatusKind.Success && model.Length > 0)
        {
            GovernorPillText.Text = model;
            GovernorPillText.ClearValue(TextBlock.ForegroundProperty);
        }
        else
        {
            GovernorPillText.Text = word;
            GovernorPillText.Foreground = brush;
        }
        AutomationProperties.SetName(StatusPillButton, $"{word}, {model}");

        PopoverStatusIcon.Glyph = Themes.StatusStyle.GlyphOf(kind);
        PopoverStatusIcon.Foreground = brush;
        PopoverStatusWord.Text = word + (GovernorViewModel.Throttled ? " · high load" : "");
        PopoverStatusWord.Foreground = brush;
        PopoverModelName.Text = model.Length > 0 ? model : "No model";
        PopoverModelDetail.Text = string.IsNullOrEmpty(GovernorViewModel.ConfiguredModel)
            ? "main"
            : $"main · {System.IO.Path.GetFileName(GovernorViewModel.ConfiguredModel.Replace('\\', '/'))}";

        var fastId = ModelsViewModel.FastSelection;
        var fast = ModelsViewModel.Models.FirstOrDefault(m => m.Id == fastId);
        PopoverFastModel.Text = "Fast: " + (fast is not null ? $"{ShortModelName(fast.Name)} · {fast.SizeDisplay}"
            : string.IsNullOrEmpty(fastId) ? "none selected" : ShortModelName(fastId));
    }

    /// <summary>VRAM and GPU for the pill and the popover, polled every few seconds (the
    /// awareness snapshot is what the HUD's meters read too).</summary>
    private void StartTelemetry()
    {
        var timer = DispatcherQueue.CreateTimer();
        timer.Interval = TimeSpan.FromSeconds(4);
        timer.Tick += async (_, _) => await PollTelemetryAsync();
        timer.Start();
        Closed += (_, _) => timer.Stop();
    }

    private bool _telemetryBusy;

    private async Task PollTelemetryAsync()
    {
        if (_telemetryBusy || GovernorViewModel.Status == "offline") return;
        _telemetryBusy = true;
        try
        {
            var status = await _api.FetchAwarenessStatusAsync();
            ApplyTelemetry(status.Snapshot);
        }
        catch
        {
            // The governor poll already shows the backend as offline.
        }
        finally
        {
            _telemetryBusy = false;
        }
    }

    private void ApplyTelemetry(AwarenessSnapshot snapshot)
    {
        if (!snapshot.GpuAvailable || snapshot.VramTotalMb <= 0)
        {
            StatusVramText.Text = "";
            PopoverVramText.Text = "No GPU";
            PopoverGpuText.Text = "—";
            PopoverVramScale.ScaleX = 0;
            PopoverGpuScale.ScaleX = 0;
            return;
        }
        var usedGb = snapshot.VramUsedMb / 1024.0;
        var totalGb = snapshot.VramTotalMb / 1024.0;
        StatusVramText.Text = $"{usedGb:0.0} GB";
        PopoverVramText.Text = $"{usedGb:0.0} / {totalGb:0.0} GB";
        PopoverGpuText.Text = $"{snapshot.GpuUtilPercent:0}%";
        PopoverVramScale.ScaleX = Math.Clamp(snapshot.VramUsedMb / snapshot.VramTotalMb, 0, 1);
        PopoverGpuScale.ScaleX = Math.Clamp(snapshot.GpuUtilPercent / 100.0, 0, 1);
        // Same soft limits as the HUD's meters.
        PopoverVramBar.Fill = Themes.JarvisTheme.Brush(snapshot.VramUtilPercent >= 88 ? "MeterHigh" : "MeterNormal");
        PopoverGpuBar.Fill = Themes.JarvisTheme.Brush(snapshot.GpuUtilPercent >= 90 ? "MeterHigh" : "MeterNormal");
    }

    private void StatusPill_Click(object sender, RoutedEventArgs e) =>
        SetStatusPopoverOpen(StatusPopover.Visibility != Visibility.Visible);

    /// <summary>Opens the popover under the pill, right edges aligned. Glass can't frost XAML,
    /// so the transcript's text fades while it is open, as the window's does under a sheet.</summary>
    private void SetStatusPopoverOpen(bool open)
    {
        if (open == (StatusPopover.Visibility == Visibility.Visible)) return;
        if (open)
        {
            SetWorkspaceDropdownOpen(false);
            var pill = StatusCapsule.TransformToVisual(RootGrid).TransformBounds(
                new Windows.Foundation.Rect(0, 0, StatusCapsule.ActualWidth, StatusCapsule.ActualHeight));
            var left = Math.Max(8, pill.Right - StatusPopover.Width);
            StatusPopover.Margin = new Thickness(left, pill.Bottom + 8, 0, 0);
            _ = PollTelemetryAsync();
        }
        StatusPopover.Visibility = open ? Visibility.Visible : Visibility.Collapsed;
        _ = Motion(SurfaceContent, affectsGlass: false).AnimateTo(0, 0, open ? PopoverBackdropOpacity : 1.0, Backdrop);
    }

    /// <summary>What the transcript's text fades to under the status popover.</summary>
    private const double PopoverBackdropOpacity = 0.15;

    private void StatusModels_Click(object sender, RoutedEventArgs e)
    {
        SetStatusPopoverOpen(false);
        Settings_Click(sender, e);
    }

    // ------------------------------------------------------------------ inspector auto-open

    private const int ArtifactsTab = 0;
    private const int ContextTab = 3;
    private string? _inspectorAutoOpenedFor;

    /// <summary>Opens the inspector on <paramref name="tab"/> the first time a chat has
    /// something for it; after that (or once the user has closed it) it stays their call.</summary>
    private async void AutoOpenInspector(int tab)
    {
        var chat = ChatViewModel.ActiveSessionId ?? "";
        if (_inspectorAutoOpenedFor == chat) return;
        _inspectorAutoOpenedFor = chat;
        RightPanelTabs.SelectedIndex = tab;
        if (!RightPanelViewModel.IsOpen) RightPanelViewModel.Toggle();
        if (tab == ArtifactsTab) await RightPanelViewModel.RefreshArtifactsAsync();
    }

    private async void GovernorToggle_Toggled(object sender, RoutedEventArgs e)
    {
        if (GovernorViewModel is null) return; // IsOn="True" in XAML fires this inside InitializeComponent
        if (GovernorToggle.IsOn) await GovernorViewModel.ResumeAsync();
        else await GovernorViewModel.PauseAsync("User requested manual pause");
    }

    private void RefreshActivityList()
    {
        ActivityStepsList.Items.Clear();
        foreach (var step in ChatViewModel.ActivitySteps)
        {
            // Status as icon + word + colour, on a tertiary-fill row.
            var kind = Themes.StatusStyle.FromStepStatus(step.Status);
            var statusBrush = new Microsoft.UI.Xaml.Media.SolidColorBrush(Themes.StatusStyle.ColorOf(kind));

            var row = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8 };
            row.Children.Add(new FontIcon
            {
                Style = Themes.JarvisTheme.Style("JarvisIcon"),
                Glyph = Themes.StatusStyle.GlyphOf(kind, inProgress: kind == Themes.StatusKind.Warning),
                FontSize = 12,
                Foreground = statusBrush,
                VerticalAlignment = VerticalAlignment.Center,
            });
            row.Children.Add(new TextBlock { Text = step.Tool, Style = Themes.JarvisTheme.Style("BodyText"), VerticalAlignment = VerticalAlignment.Center });
            row.Children.Add(new TextBlock { Text = step.Status, Style = Themes.JarvisTheme.Style("CaptionEmphasisText"), Foreground = statusBrush, VerticalAlignment = VerticalAlignment.Center });

            var card = new Border
            {
                Background = Themes.JarvisTheme.Brush("FillTertiary"),
                CornerRadius = new CornerRadius(10),
                Padding = new Thickness(8, 6, 8, 6),
                Child = row,
            };
            ActivityStepsList.Items.Add(card);
        }
    }

    private Jarvis.Core.Models.ChatMessage? _boundReasoningMessage;

    /// <summary>Follows ChatViewModel.ActiveReasoningMessage as it switches between the in-flight
    /// streaming message and the last-completed one, resubscribing to that message's own
    /// PropertyChanged so the tab updates live token-by-token while reasoning streams -- the panel
    /// never opens itself for this, only the badge (UpdateReasoningBadge) reacts.</summary>
    private void RebindReasoningMessage()
    {
        if (_boundReasoningMessage is not null)
        {
            _boundReasoningMessage.PropertyChanged -= OnReasoningMessagePropertyChanged;
        }

        _boundReasoningMessage = ChatViewModel.ActiveReasoningMessage;
        if (_boundReasoningMessage is not null)
        {
            _boundReasoningMessage.PropertyChanged += OnReasoningMessagePropertyChanged;
        }

        UpdateReasoningText();
    }

    private void OnReasoningMessagePropertyChanged(object? sender, System.ComponentModel.PropertyChangedEventArgs args)
    {
        if (args.PropertyName == nameof(Jarvis.Core.Models.ChatMessage.ReasoningContent))
        {
            DispatcherQueue.TryEnqueue(UpdateReasoningText);
        }
    }

    private void UpdateReasoningText()
    {
        var text = _boundReasoningMessage?.ReasoningContent;
        ReasoningText.Text = string.IsNullOrEmpty(text) ? "No reasoning for this turn yet." : text;
    }

    private void RightPanelTabs_SelectionChanged(object sender, RoutedEventArgs e)
    {
        var pages = new[] { ArtifactsPage, FilesPage, ReasoningPage, ContextPage, ActivityPage };
        for (var i = 0; i < pages.Length; i++)
        {
            pages[i].Visibility = i == RightPanelTabs.SelectedIndex ? Visibility.Visible : Visibility.Collapsed;
        }
    }

    private void UpdateReasoningBadge()
    {
        ReasoningBadge.Visibility = ChatViewModel.IsReasoningStreaming ? Visibility.Visible : Visibility.Collapsed;
    }

    private void UpdateContextTab()
    {
        var retrieval = ChatViewModel.LatestRetrieval;
        if (retrieval is null)
        {
            ContextBudgetHeader.Text = "No turn yet";
            RetrievedChunksList.Items.Clear();
            return;
        }

        var budget = retrieval.BudgetReport;
        ContextBudgetHeader.Text = $"{budget.TotalInputTokensUsed} / {budget.AvailableInputBudget} tokens";
        Tier1Text.Text = $"Tier1 System: {budget.Tier1SystemTokens}";
        Tier2Text.Text = $"Tier2 User+Files: {budget.Tier2UserTokens}";
        Tier3Text.Text = $"Tier3 RAG: {budget.Tier3RagTokens}";
        Tier4Text.Text = $"Tier4 History: {budget.Tier4HistoryTokens}";

        RetrievedChunksList.Items.Clear();
        foreach (var chunk in retrieval.ChunksUsed)
        {
            var panel = new StackPanel { Spacing = 2 };
            panel.Children.Add(new TextBlock
            {
                Text = $"{chunk.FileName ?? chunk.FilePath} L{chunk.StartLine}-L{chunk.EndLine}",
                Style = Themes.JarvisTheme.Style("BodyText"),
            });
            if (!string.IsNullOrEmpty(chunk.SymbolName))
            {
                panel.Children.Add(new TextBlock
                {
                    Text = chunk.SymbolName,
                    Style = Themes.JarvisTheme.Style("DataText"),
                });
            }
            RetrievedChunksList.Items.Add(panel);
        }

        if (retrieval.ChunksDropped.Count > 0)
        {
            RetrievedChunksList.Items.Add(new TextBlock
            {
                Text = $"{retrieval.ChunksDropped.Count} dropped (budget)",
                Style = Themes.JarvisTheme.Style("FootnoteText"),
                Foreground = Themes.JarvisTheme.Brush("StatusWarning"),
            });
        }
    }

    private void UpdateErrorBanner()
    {
        var error = ChatViewModel.Error;
        ErrorBanner.IsOpen = !string.IsNullOrEmpty(error);
        ErrorBanner.Message = error ?? "";
        ErrorBanner.Title = "Something went wrong";
    }

    private void NewChat_Click(object sender, RoutedEventArgs e) => ChatViewModel.NewChat();

    // ---- Spaces and ephemeral mode ---------------------------------------------------------

    /// <summary>True while UpdateSpaceChrome writes the switch/toggle back from the view model:
    /// both glass controls raise their change events for programmatic changes too.</summary>
    private bool _syncingSpaceChrome;

    private async void SpaceSwitch_SelectionChanged(object sender, RoutedEventArgs e)
    {
        // SelectedIndex="1" in XAML fires this inside InitializeComponent.
        if (ChatViewModel is null || _syncingSpaceChrome) return;
        var target = SpaceSwitch.SelectedIndex == 0 ? ChatSpace.Freeform : ChatSpace.Workspace;
        await SwitchSpaceAnimatedAsync(target);
    }

    private async void EphemeralButton_Click(object sender, RoutedEventArgs e) =>
        await ChatViewModel.SetEphemeralAsync(!ChatViewModel.IsEphemeral);

    private const string ModelChoiceSettingKey = "ModelChoice";
    private static readonly string?[] ModelChoices = { null, "main", "fast" }; // Auto | Main | Fast
    private bool _syncingModelChoice;

    /// <summary>Restores the saved Auto | Main | Fast choice into the switch and the chat.</summary>
    private void RestoreModelChoice()
    {
        string? saved = null;
        try
        {
            saved = Windows.Storage.ApplicationData.Current.LocalSettings.Values[ModelChoiceSettingKey] as string;
        }
        catch (Exception ex)
        {
            App.Log($"reading saved model choice failed: {ex.GetType().Name}: {ex.Message}");
        }
        var index = Math.Max(0, Array.IndexOf(ModelChoices, saved));
        _syncingModelChoice = true;
        ModelChoiceSwitch.SelectedIndex = index;
        _syncingModelChoice = false;
        ApplyModelChoice(index);
    }

    private void ModelChoiceSwitch_SelectionChanged(object sender, RoutedEventArgs e)
    {
        if (_syncingModelChoice) return;
        var index = Math.Clamp(ModelChoiceSwitch.SelectedIndex, 0, ModelChoices.Length - 1);
        ApplyModelChoice(index);
        try
        {
            Windows.Storage.ApplicationData.Current.LocalSettings.Values[ModelChoiceSettingKey] = ModelChoices[index] ?? "auto";
        }
        catch (Exception ex)
        {
            App.Log($"saving model choice failed: {ex.GetType().Name}: {ex.Message}");
        }
    }

    private void ApplyModelChoice(int index)
    {
        ChatViewModel.ModelChoice = ModelChoices[index];
        ModelChoiceNote.Text = index switch
        {
            1 => "Every turn on the main model; switching from fast takes a few seconds",
            2 => "Every turn on the fast model; switching loads it (a few seconds). Images still go to main",
            _ => "Quick turns run on the loaded model, thinking off",
        };
    }

    private const string SpaceSettingKey = "ChatSpace";

    private static ChatSpace LoadSavedSpace()
    {
        try
        {
            return Windows.Storage.ApplicationData.Current.LocalSettings.Values[SpaceSettingKey] is string saved
                && Enum.TryParse<ChatSpace>(saved, out var space) ? space : ChatSpace.Workspace;
        }
        catch (Exception ex)
        {
            App.Log($"reading saved space failed: {ex.GetType().Name}: {ex.Message}");
            return ChatSpace.Workspace;
        }
    }

    private static void SaveSpace(ChatSpace space)
    {
        try
        {
            Windows.Storage.ApplicationData.Current.LocalSettings.Values[SpaceSettingKey] = space.ToString();
        }
        catch (Exception ex)
        {
            App.Log($"saving space failed: {ex.GetType().Name}: {ex.Message}");
        }
    }

    /// <summary>Brings the sidebar and header in line with the chat's space and ephemeral state:
    /// the switch and toggle positions, the workspace picker (Workspace only), the header label,
    /// and the right panel's project (none in Freeform, so it shows no project files).</summary>
    private void UpdateSpaceChrome()
    {
        var freeform = ChatViewModel.Space == ChatSpace.Freeform;
        _syncingSpaceChrome = true;
        try
        {
            SpaceSwitch.SelectedIndex = freeform ? 0 : 1;
        }
        finally
        {
            _syncingSpaceChrome = false;
        }
        WorkspaceButton.Visibility = freeform ? Visibility.Collapsed : Visibility.Visible;
        if (freeform) SetWorkspaceDropdownOpen(false);
        // Ephemeral: the toolbar's eye lights up, and the line under the composer says so.
        EphemeralIcon.Foreground = ChatViewModel.IsEphemeral
            ? Themes.JarvisTheme.Brush("Accent")
            : Themes.JarvisTheme.Brush("Label");
        ToolTipService.SetToolTip(EphemeralButton, ChatViewModel.IsEphemeral
            ? "Ephemeral chat is on: nothing is saved"
            : "Ephemeral chat: nothing is saved");
        UpdateComposerCaption();
        RightPanelViewModel.ProjectId = ChatViewModel.TurnProjectId;
        UpdateSessionHighlight();
    }

    private void WorkspaceButton_Click(object sender, RoutedEventArgs e)
    {
        if (WorkspaceDropdown.Visibility == Visibility.Visible)
        {
            SetWorkspaceDropdownOpen(false);
            return;
        }
        // Float the card just under the button, matching its width.
        var origin = WorkspaceButton.TransformToVisual(RootGrid).TransformPoint(new Windows.Foundation.Point(0, WorkspaceButton.ActualHeight + 6));
        _workspaceDropdownOrigin = origin;
        WorkspaceDropdown.Margin = new Thickness(origin.X, origin.Y, 0, 0);
        WorkspaceDropdown.MinWidth = WorkspaceButton.ActualWidth;
        SetWorkspaceDropdownOpen(true);
        UpdateWorkspaceHighlight();
    }

    /// <summary>Every slab's glass renders under all XAML, so the session rows' plain
    /// TextBlocks would print straight through the card (it read as a stale frame). The card
    /// always floats over the sidebar's list, so the list is collapsed while it is open --
    /// collapsed rather than transparent, because opacity leaves the rows' glass in the scene
    /// and an empty pill would peek out under the card. The buttons' labels are glass text and
    /// need nothing; the list is in a * row so nothing else moves.</summary>
    private void SetWorkspaceDropdownOpen(bool open)
    {
        WorkspaceDropdown.Visibility = open ? Visibility.Visible : Visibility.Collapsed;
        var listVisibility = open ? Visibility.Collapsed : Visibility.Visible;
        SessionsList.Visibility = listVisibility;
        SessionSearch.Opacity = open ? 0 : 1; // opacity, so the list doesn't jump under the card
        SessionSearchIcon.Opacity = open ? 0 : 1;
    }

    /// <summary>Light dismiss: any press outside the card (or its button) closes it. Registered
    /// with handledEventsToo so presses swallowed by controls still count.</summary>
    private void RootGrid_PointerPressedForDropdown(object sender, Microsoft.UI.Xaml.Input.PointerRoutedEventArgs e)
    {
        if (e.OriginalSource is not DependencyObject source) return;
        if (StatusPopover.Visibility == Visibility.Visible
            && !IsInside(source, StatusPopover) && !IsInside(source, StatusCapsule))
        {
            SetStatusPopoverOpen(false);
        }
        if (WorkspaceDropdown.Visibility != Visibility.Visible) return;
        if (IsInside(source, WorkspaceDropdown) || IsInside(source, WorkspaceButton)) return;
        SetWorkspaceDropdownOpen(false);
    }

    private static bool IsInside(DependencyObject node, DependencyObject ancestor)
    {
        DependencyObject? n = node;
        while (n is not null)
        {
            if (ReferenceEquals(n, ancestor)) return true;
            n = Microsoft.UI.Xaml.Media.VisualTreeHelper.GetParent(n);
        }
        return false;
    }

    private bool _syncingWorkspaceMenu;

    /// <summary>
    /// The dropdown leans with the puck: while the pill is dragged, the card translates by a
    /// damped fraction of the pull (more when pushed past the last row, a little mid-list), and
    /// springs back with it on release. The puck reports the pull; this just applies it.
    /// </summary>
    private void WorkspaceMenu_DragPullChanged(object? sender, float pull)
    {
        // Rubber-band: the further you pull, the less the card gives.
        var sign = MathF.Sign(pull);
        var magnitude = MathF.Abs(pull);
        var eased = 28f * (1f - MathF.Exp(-magnitude / 28f)); // asymptote at 28 DIPs
        var offset = sign * eased * 0.6f;
        // Through layout, not a RenderTransform: the glass slab publishes its bounds to the
        // scene on LayoutUpdated, and a render transform would move the XAML without the glass.
        var m = _workspaceDropdownOrigin;
        WorkspaceDropdown.Margin = new Thickness(m.X, m.Y + offset, 0, 0);
    }

    private Windows.Foundation.Point _workspaceDropdownOrigin;

    private async void WorkspaceMenu_SelectionChanged(object sender, RoutedEventArgs e)
    {
        if (_syncingWorkspaceMenu) return;
        var index = WorkspaceMenu.SelectedIndex;
        if (index < 0 || index >= ProjectsViewModel.Projects.Count) return;
        var project = ProjectsViewModel.Projects[index];
        // Let the puck land before the card folds away.
        await Task.Delay(180);
        SetWorkspaceDropdownOpen(false);
        if (project.Id != ProjectsViewModel.ActiveProject?.Id)
        {
            await ProjectsViewModel.ActivateAsync(project);
        }
    }

    /// <summary>Rebuilds the menu's rows from the project list and parks the puck on the active
    /// one (without that counting as a user selection).</summary>
    private void UpdateWorkspaceHighlight()
    {
        WorkspaceButton.Text = ProjectsViewModel.ActiveProject?.Name ?? "Default Workspace";
        _syncingWorkspaceMenu = true;
        try
        {
            var names = ProjectsViewModel.Projects.Select(p => p.Name.Replace('|', '/')).ToList();
            var items = names.Count > 0 ? string.Join("|", names) : "Default Workspace";
            if (WorkspaceMenu.Items != items) WorkspaceMenu.Items = items;
            var index = ProjectsViewModel.Projects.ToList().FindIndex(p => p.Id == ProjectsViewModel.ActiveProject?.Id);
            WorkspaceMenu.SelectedIndex = Math.Max(0, index);
        }
        finally
        {
            _syncingWorkspaceMenu = false;
        }
    }

    private async void SessionItem_Tapped(object sender, Microsoft.UI.Xaml.Input.TappedRoutedEventArgs e)
    {
        if (sender is FrameworkElement { Tag: Session session })
        {
            await SessionsViewModel.SelectAsync(session);
            UpdateSessionHighlight();
        }
    }

    /// <summary>x:Bind target for the session rows (the template can't call the view model's
    /// instance-free helper with the whole item).</summary>
    public static string SessionRowLabel(string sessionId, string? lastMessage) =>
        SessionsViewModel.DisplayLabel(new Session { SessionId = sessionId, LastMessage = lastMessage });

    /// <summary>The active session's row sits on fill-secondary with its icon in accent;
    /// called whenever the active session or the list changes, and per realised container.</summary>
    private void UpdateSessionHighlight()
    {
        foreach (var group in _sessionGroups)
        {
            foreach (var item in group)
            {
                if (SessionsList.ContainerFromItem(item) is ListViewItem container
                    && container.ContentTemplateRoot is Grid row)
                {
                    ApplySessionHighlight(row, item);
                }
            }
        }
    }

    private void ApplySessionHighlight(Grid row, Session session)
    {
        var active = session.SessionId == ChatViewModel.ActiveSessionId;
        row.Background = active ? Themes.JarvisTheme.Brush("FillSecondary") : new Microsoft.UI.Xaml.Media.SolidColorBrush(Colors.Transparent);
        if (row.Children[0] is FontIcon icon)
        {
            icon.Foreground = Themes.JarvisTheme.Brush(active ? "Accent" : "LabelSecondary");
        }
        if (row.Children[2] is Button more) more.Opacity = 0;
    }

    /// <summary>Hover: a faint fill (unless it is the active row) and the "..." button.</summary>
    private void SessionRow_PointerEntered(object sender, PointerRoutedEventArgs e)
    {
        if (sender is not Grid { Tag: Session session } row) return;
        if (session.SessionId != ChatViewModel.ActiveSessionId) row.Background = Themes.JarvisTheme.Brush("FillTertiary");
        if (row.Children[2] is Button more) more.Opacity = 1;
    }

    private void SessionRow_PointerExited(object sender, PointerRoutedEventArgs e)
    {
        // Leave "..." up while its menu is open (the pointer moves onto the menu).
        if (sender is Grid { Tag: Session session } row
            && !(row.Children[2] is Button { Flyout: Microsoft.UI.Xaml.Controls.Primitives.FlyoutBase { IsOpen: true } }))
        {
            ApplySessionHighlight(row, session);
        }
    }

    private List<SessionGroup> _sessionGroups = new();

    /// <summary>Regroups the session list by date and applies the search; the ListView reads
    /// the groups through a CollectionViewSource.</summary>
    private void RebuildSessionGroups()
    {
        _sessionGroups = SessionGroup.Build(SessionsViewModel.Sessions, SessionSearch.Text, DateTime.Now);
        SessionsList.ItemsSource = new Microsoft.UI.Xaml.Data.CollectionViewSource
        {
            Source = _sessionGroups,
            IsSourceGrouped = true,
        }.View;
    }

    private void SessionSearch_TextChanged(object sender, TextChangedEventArgs e) => RebuildSessionGroups();

    private async void DeleteSession_Click(object sender, RoutedEventArgs e)
    {
        if (sender is FrameworkElement { Tag: Session session })
        {
            await SessionsViewModel.DeleteAsync(session);
        }
    }

    private async void FreeVram_Click(object sender, RoutedEventArgs e)
    {
        try { await _api.UnloadModelsAsync(); } catch { /* surfaced via governor poll */ }
    }

    private void Settings_Click(object sender, RoutedEventArgs e)
    {
        if (SettingsHost.Children.Count > 0) return;
        var pane = new SettingsPane(_api, GovernorViewModel, PersonaViewModel, RoutinesViewModel, ModelsViewModel);
        pane.CloseRequested += () => CloseSheet(pane.SheetSurface);
        OpenSheet(pane, pane.SheetSurface);
    }

    private void NewWorkspace_Click(object sender, RoutedEventArgs e)
    {
        SetWorkspaceDropdownOpen(false);
        if (SettingsHost.Children.Count > 0) return;
        var pane = new NewWorkspacePane(ProjectsViewModel, WinRT.Interop.WindowNative.GetWindowHandle(this));
        pane.CloseRequested += () => CloseSheet(pane.SheetSurface);
        // Nothing else to wire: the pane activates the new project, and ActiveProjectChanged
        // rescopes the chat and rebuilds the dropdown's rows.
        OpenSheet(pane, pane.SheetSurface);
    }

    private void HudButton_Click(object sender, RoutedEventArgs e) => Hud?.ToggleVisible();

    private async void MicButton_Click(object sender, RoutedEventArgs e)
    {
        if (!VoiceViewModel.IsActive) await VoiceViewModel.StartAsync();
        else VoiceViewModel.Stop();
        UpdateVoiceStateText();
    }

    /// <summary>The mic button lights while voice is on, and the line under the composer says
    /// what it is doing (listening, working, speaking).</summary>
    private void UpdateVoiceStateText()
    {
        var unavailable = !VoiceViewModel.IsSupported && !string.IsNullOrEmpty(VoiceViewModel.ErrorMessage);
        MicButton.IsEnabled = !unavailable;
        ToolTipService.SetToolTip(MicButton, unavailable ? "Mic unavailable" : VoiceViewModel.IsActive ? "Stop talking" : "Talk to Jarvis");
        MicIcon.Foreground = Themes.JarvisTheme.Brush(VoiceViewModel.IsActive ? "Accent" : unavailable ? "LabelTertiary" : "Label");
        UpdateComposerCaption();
    }

    /// <summary>One line under the composer: the voice state while the mic is on, else whether
    /// this chat is kept, else where the model runs.</summary>
    private void UpdateComposerCaption()
    {
        if (VoiceViewModel.IsActive)
        {
            ComposerCaption.Text = VoiceViewModel.State switch
            {
                Jarvis.Core.Models.VoiceState.Listening => "Listening",
                Jarvis.Core.Models.VoiceState.Armed => "Go ahead",
                Jarvis.Core.Models.VoiceState.Thinking => "Working",
                Jarvis.Core.Models.VoiceState.Speaking => "Speaking",
                _ => "Voice on",
            };
            ComposerCaption.Foreground = Themes.JarvisTheme.Brush("Accent");
            return;
        }
        ComposerCaption.Foreground = Themes.JarvisTheme.Brush("LabelTertiary");
        ComposerCaption.Text = ChatViewModel.IsEphemeral
            ? "Ephemeral chat · nothing from it is saved"
            : "Runs on this PC · nothing leaves it";
    }

    private async Task HandleVoiceCommandAsync(string query)
    {
        ComposerBox.Text = "";
        await ChatViewModel.SendMessageAsync(query);
    }

    private async void ToggleRightPanel_Click(object sender, RoutedEventArgs e)
    {
        RightPanelViewModel.Toggle();
        if (RightPanelViewModel.IsOpen)
        {
            await RightPanelViewModel.RefreshArtifactsAsync();
        }
    }

    private async void RefreshArtifacts_Click(object sender, RoutedEventArgs e) => await RightPanelViewModel.RefreshArtifactsAsync();

    private async void RefreshFiles_Click(object sender, RoutedEventArgs e) => await RightPanelViewModel.RefreshFilesAsync();

    private async void ArtifactItem_Tapped(object sender, Microsoft.UI.Xaml.Input.TappedRoutedEventArgs e)
    {
        if (sender is FrameworkElement { Tag: Artifact artifact })
        {
            await RightPanelViewModel.SelectArtifactAsync(artifact);
        }
    }

    private async void SendButton_Click(object sender, RoutedEventArgs e) => await SendCurrentAsync();

    private async void ComposerBox_KeyDown(object sender, KeyRoutedEventArgs e)
    {
        var shiftDown = InputKeyboardSource.GetKeyStateForCurrentThread(VirtualKey.Shift)
            .HasFlag(Windows.UI.Core.CoreVirtualKeyStates.Down);
        if (e.Key == VirtualKey.Enter && !shiftDown)
        {
            e.Handled = true;
            await SendCurrentAsync();
            return;
        }

        // Ctrl+V with a bitmap on the clipboard (a Win+Shift+S capture) attaches it as an image
        // instead of pasting nothing; text on the clipboard still pastes as text.
        var ctrlDown = InputKeyboardSource.GetKeyStateForCurrentThread(VirtualKey.Control)
            .HasFlag(Windows.UI.Core.CoreVirtualKeyStates.Down);
        if (e.Key == VirtualKey.V && ctrlDown)
        {
            var content = Windows.ApplicationModel.DataTransfer.Clipboard.GetContent();
            if (content.Contains(Windows.ApplicationModel.DataTransfer.StandardDataFormats.Bitmap)
                && !content.Contains(Windows.ApplicationModel.DataTransfer.StandardDataFormats.Text))
            {
                e.Handled = true;
                var saved = await ImageAttachmentService.SaveClipboardImageAsync();
                if (saved is not null)
                {
                    _pendingAttachmentPaths.Add(saved);
                    AddAttachmentChip(System.IO.Path.GetFileName(saved), saved);
                }
            }
        }
    }

    private readonly List<string> _pendingAttachmentPaths = new();

    private async Task SendCurrentAsync()
    {
        var text = ComposerBox.Text.Trim();
        if (string.IsNullOrEmpty(text) && _pendingAttachmentPaths.Count == 0) return;

        ComposerBox.Text = "";
        var paths = _pendingAttachmentPaths.ToList();
        _pendingAttachmentPaths.Clear();
        AttachmentChips.Items.Clear();
        VisionHint.Visibility = Visibility.Collapsed;

        List<Attachment>? uploaded = null;
        if (paths.Count > 0)
        {
            SendButton.IsEnabled = false;
            ComposerBox.Placeholder = "Uploading attachments…";
            uploaded = new List<Attachment>();
            foreach (var path in paths)
            {
                try
                {
                    // Big screenshots are downscaled first; the projector's token cost grows with
                    // resolution long after the text stopped getting more legible.
                    var toUpload = await ImageAttachmentService.PrepareForUploadAsync(path);
                    uploaded.Add(await _api.UploadAttachmentAsync(toUpload, ChatViewModel.ActiveSessionId, ChatViewModel.TurnProjectId, ChatViewModel.ChatMode, ChatViewModel.IsEphemeral));
                }
                catch (Exception ex)
                {
                    // The turn still goes out without this file, but silently dropping it made
                    // "why can't it see my image?" impossible to diagnose. Say what happened.
                    ChatViewModel.Messages.Add(new ChatMessage
                    {
                        Role = MessageRole.System,
                        Content = $"Attachment '{System.IO.Path.GetFileName(path)}' was not uploaded: {ex.Message}",
                    });
                }
            }
            SendButton.IsEnabled = true;
            ComposerBox.Placeholder = "Message Jarvis";
        }

        await ChatViewModel.SendMessageAsync(text, uploaded);
    }

    private async void AttachFile_Click(object sender, RoutedEventArgs e)
    {
        var picker = new Windows.Storage.Pickers.FileOpenPicker();
        WinRT.Interop.InitializeWithWindow.Initialize(picker, WinRT.Interop.WindowNative.GetWindowHandle(this));
        picker.FileTypeFilter.Add("*");
        picker.ViewMode = Windows.Storage.Pickers.PickerViewMode.List;

        var files = await picker.PickMultipleFilesAsync();
        foreach (var file in files)
        {
            _pendingAttachmentPaths.Add(file.Path);
            AddAttachmentChip(file.Name, file.Path);
        }
    }

    private void AddAttachmentChip(string name, string path)
    {
        var isImage = ImageAttachmentService.IsImagePath(path);
        var chip = new Border
        {
            Background = Themes.JarvisTheme.Brush("FillSecondary"),
            CornerRadius = new CornerRadius(isImage ? 14 : 999),
            Padding = new Thickness(isImage ? 4 : 10, 4, 6, 4),
        };
        var row = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 6 };
        if (isImage)
        {
            // A thumbnail says "this is the picture I mean" far better than a filename.
            try
            {
                var bitmap = new Microsoft.UI.Xaml.Media.Imaging.BitmapImage(new Uri(path)) { DecodePixelHeight = 40 };
                row.Children.Add(new Border
                {
                    CornerRadius = new CornerRadius(10),
                    Width = 56,
                    Height = 40,
                    Child = new Image { Source = bitmap, Stretch = Microsoft.UI.Xaml.Media.Stretch.UniformToFill },
                });
            }
            catch
            {
                // unreadable file: fall through to the name-only chip
            }
        }
        row.Children.Add(new TextBlock { Text = name, Style = Themes.JarvisTheme.Style("FootnoteText"), VerticalAlignment = VerticalAlignment.Center });
        var removeButton = new Button
        {
            Content = new FontIcon { Style = Themes.JarvisTheme.Style("JarvisIcon"), Glyph = "", FontSize = 12, Foreground = Themes.JarvisTheme.Brush("LabelSecondary") },
            Padding = new Thickness(4),
            Background = new Microsoft.UI.Xaml.Media.SolidColorBrush(Colors.Transparent),
            BorderThickness = new Thickness(0),
        };
        ToolTipService.SetToolTip(removeButton, "Remove");
        removeButton.Click += (_, _) =>
        {
            _pendingAttachmentPaths.Remove(path);
            AttachmentChips.Items.Remove(chip);
            _ = RefreshVisionHintAsync();
        };
        row.Children.Add(removeButton);
        chip.Child = row;
        AttachmentChips.Items.Add(chip);
        _ = RefreshVisionHintAsync();
    }

    /// <summary>
    /// Below the chips, one line saying what will happen to an attached image: seen by the model
    /// (a projector is loaded or paired with the main model), described by the CPU captioner, or
    /// only attached as a file. Hidden when no image is attached.
    /// </summary>
    private async Task RefreshVisionHintAsync()
    {
        if (!_pendingAttachmentPaths.Any(ImageAttachmentService.IsImagePath))
        {
            VisionHint.Visibility = Visibility.Collapsed;
            return;
        }
        VisionHint.Text = "Checking whether the model can see images…";
        VisionHint.Visibility = Visibility.Visible;
        string text;
        try
        {
            var catalog = await _api.FetchModelCatalogAsync();
            string? seeingModel = null;
            if (catalog.LoadedProjector is not null && catalog.LoadedSlot is not null
                && catalog.Selection.TryGetValue(catalog.LoadedSlot, out var loadedId))
            {
                seeingModel = catalog.Models.FirstOrDefault(m => m.Id == loadedId)?.Name ?? loadedId;
            }
            else if (catalog.Selection.TryGetValue("main", out var mainId) && mainId is not null)
            {
                var main = catalog.Models.FirstOrDefault(m => m.Id == mainId);
                if (main?.Projector is not null) seeingModel = main.Name;
            }

            if (seeingModel is not null)
                text = $"{seeingModel} will see the image directly.";
            else if (catalog.Captioner?.Available == true)
                text = "The current model cannot see images; the local captioner will describe them for it.";
            else
                text = "The current model cannot see images and no captioner is installed — they will be attached as files only.";
        }
        catch
        {
            text = "Image attached.";
        }
        if (_pendingAttachmentPaths.Any(ImageAttachmentService.IsImagePath))
            VisionHint.Text = text;
    }

    /// <summary>
    /// ListView reuses containers, so this hooks each MessageBubbleControl's approve/deny events
    /// exactly once (Loaded can fire more than once per instance as it's recycled) rather than via
    /// x:Bind, since the control renders through DataContext rather than compiled bindings.
    /// </summary>
    private void MessageBubble_Loaded(object sender, RoutedEventArgs e)
    {
        if (sender is not MessageBubbleControl bubble || bubble.Tag is bool) return;
        bubble.Tag = true;
        bubble.ApproveRequested += async confirmation => await ChatViewModel.ConfirmActionAsync(confirmation);
        bubble.DenyRequested += confirmation => ChatViewModel.DenyAction(confirmation);
    }
}
