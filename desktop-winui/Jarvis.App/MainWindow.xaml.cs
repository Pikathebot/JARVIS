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
        GovernorViewModel.Start();
        AwarenessViewModel.Start();

        ChatViewModel.PropertyChanged += (_, args) =>
        {
            if (args.PropertyName == nameof(ChatViewModel.Error))
            {
                DispatcherQueue.TryEnqueue(UpdateErrorBanner);
            }
            else if (args.PropertyName == nameof(ChatViewModel.LatestRetrieval))
            {
                DispatcherQueue.TryEnqueue(UpdateContextTab);
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
            RightPanelViewModel.ProjectId = project?.Id;
            SessionLabel.Text = project?.Name ?? "Default Workspace";
            UpdateWorkspaceHighlight();
            _ = SessionsViewModel.RefreshAsync();
        };
        ChatViewModel.MessageCompleted += completedMessage => DispatcherQueue.TryEnqueue(() => _ = SessionsViewModel.RefreshAsync());
        ChatViewModel.PropertyChanged += (_, args) =>
        {
            if (args.PropertyName == nameof(ViewModels.ChatViewModel.ActiveSessionId)) DispatcherQueue.TryEnqueue(UpdateSessionHighlight);
        };

        // Initial state is fetched when the backend is reachable, not now: the window is built
        // before BackendHost has uvicorn up, so a fetch here fails silently and nothing would
        // retry it -- the sidebar stayed empty and the workspace picker stuck on its placeholder
        // until the first completed turn. Projects first; ActiveProjectChanged scopes and
        // refreshes the session list. Also re-runs after a backend restart.
        GovernorViewModel.BackendCameOnline += () => _ = ProjectsViewModel.RefreshAsync();
        // The session list virtualises and recycles its rows now, and Loaded does not fire
        // again for a recycled container, so the active tint is applied per content change too.
        SessionsList.ContainerContentChanging += (_, args) =>
        {
            if (!args.InRecycleQueue && args.Item is Session session
                && args.ItemContainer.ContentTemplateRoot is Jarvis_Glass.GlassSlab row)
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
            appWindow.TitleBar.SetDragRectangles(new[] { new Windows.Graphics.RectInt32(0, 0, 10000, 48) });
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
        // The floating panel's top offset follows the header's height, which settles after the
        // first layout pass and changes when captions are shed.
        HeaderSlab.SizeChanged += (_, _) => ApplyResponsiveLayout(RootGrid.ActualWidth);
    }

    // ------------------------------------------------------------------ responsive layout
    //
    // Rule: the sidebar and the right panel push the chat (take their own column) whenever the
    // chat keeps at least MinChatWidth; only when it would not do they leave the grid and float
    // over the chat. So at ordinary sizes nothing ever overlaps, and a very narrow window still
    // has a usable chat with the sidebar behind ☰. The glass slabs need real layout changes
    // (they publish their bounds to the scene on LayoutUpdated), so this is code, not visual
    // states.
    private const double MinChatWidth = 360;
    private const double SidebarWidth = 240;
    private const double SidebarGap = 8;
    private const double PanelWidth = 368;
    private const double PanelGap = 8;
    private bool _compactSidebar;
    private bool _sidebarOverlayOpen;

    private void ApplyResponsiveLayout(double width)
    {
        var available = width - BodyGrid.Padding.Left - BodyGrid.Padding.Right;
        var panelOpen = RightPanelViewModel.IsOpen;

        // Sidebar docks if the chat (and an open panel, docked) still fit beside it.
        var sidebarDocked = available - (SidebarWidth + SidebarGap) >= MinChatWidth;
        var compact = !sidebarDocked;
        if (compact != _compactSidebar)
        {
            _compactSidebar = compact;
            _sidebarOverlayOpen = false;
            SidebarToggleButton.Visibility = compact ? Visibility.Visible : Visibility.Collapsed;
            if (compact)
            {
                // Out of the column, floating over the chat at the sidebar's natural width.
                SidebarColumn.Width = new GridLength(0);
                Grid.SetColumn(SidebarSlab, 1);
                SidebarSlab.HorizontalAlignment = HorizontalAlignment.Left;
                SidebarSlab.Width = SidebarWidth;
                SidebarSlab.Margin = new Thickness(0);
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

        // Right panel docks if the chat still fits beside it and the (docked) sidebar.
        var usedBySidebar = sidebarDocked ? SidebarWidth + SidebarGap : 0;
        var panelDocked = available - usedBySidebar - (PanelWidth + PanelGap) >= MinChatWidth;
        if (panelDocked)
        {
            RightPanelColumn.Width = new GridLength(panelOpen ? PanelWidth : 0);
            Grid.SetColumn(RightPanel, 2);
            RightPanel.HorizontalAlignment = HorizontalAlignment.Stretch;
            RightPanel.Width = double.NaN;
            RightPanel.Margin = new Thickness(PanelGap, 0, 0, 0);
            Canvas.SetZIndex(RightPanel, 0);
            RightPanel.ClearValue(Jarvis_Glass.GlassSlab.LayerProperty);
        }
        else
        {
            RightPanelColumn.Width = new GridLength(0);
            Grid.SetColumn(RightPanel, 1);
            RightPanel.HorizontalAlignment = HorizontalAlignment.Right;
            RightPanel.Width = Math.Min(PanelWidth, Math.Max(240, available - 24));
            // Floating, the sheet shares the chat column's cell and would stretch over the
            // header too -- covering the very Panel button that closes it. Start below the
            // header instead (its height varies with width as captions are shed).
            var headerHeight = HeaderSlab.ActualHeight > 0 ? HeaderSlab.ActualHeight + HeaderSlab.Margin.Bottom : 0;
            RightPanel.Margin = new Thickness(0, headerHeight, 0, 0);
            Canvas.SetZIndex(RightPanel, 10);
            RightPanel.Layer = 3;
        }

        // Header: shed the least important things first.
        var showCaptions = width >= 700;
        GovernorLabel.Visibility = showCaptions ? Visibility.Visible : Visibility.Collapsed;
        MicLabel.Visibility = showCaptions ? Visibility.Visible : Visibility.Collapsed;
        VoiceStateText.Visibility = showCaptions ? Visibility.Visible : Visibility.Collapsed;
        var showExtras = width >= 560;
        HudButton.Visibility = showExtras ? Visibility.Visible : Visibility.Collapsed;
        SessionLabel.Visibility = showExtras ? Visibility.Visible : Visibility.Collapsed;
        PanelButton.MinWidth = showExtras ? 80 : 56;
    }

    private void SidebarToggle_Click(object sender, RoutedEventArgs e)
    {
        if (!_compactSidebar) return;
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

    private void UpdateGovernorPill()
    {
        GovernorPillText.Text = GovernorViewModel.Status switch
        {
            "ok" => "Online",
            "throttled" => "Throttled",
            "degraded" => "Degraded",
            _ => "Offline",
        };
        GovernorPillText.Foreground = new Microsoft.UI.Xaml.Media.SolidColorBrush(GovernorViewModel.Status switch
        {
            "ok" => Colors.MediumSeaGreen,
            "throttled" => Colors.Orange,
            "degraded" => Colors.Orange,
            _ => Colors.OrangeRed,
        });

        GovernorBackendRow.Text = $"Backend: {(string.IsNullOrEmpty(GovernorViewModel.ActiveBackend) ? "—" : GovernorViewModel.ActiveBackend)}";
        GovernorModelRow.Text = $"Model: {GovernorViewModel.ConfiguredModel}";
        GovernorStatusRow.Text = $"Status: {GovernorPillText.Text}" + (GovernorViewModel.Throttled ? " (high load)" : "");
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
            var dotColor = step.Status switch
            {
                "success" => Colors.MediumSeaGreen,
                "error" => Colors.OrangeRed,
                _ => Colors.Orange,
            };

            var row = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 6 };
            row.Children.Add(new Microsoft.UI.Xaml.Shapes.Ellipse
            {
                Width = 7,
                Height = 7,
                Fill = new Microsoft.UI.Xaml.Media.SolidColorBrush(dotColor),
                VerticalAlignment = VerticalAlignment.Center,
            });
            row.Children.Add(new TextBlock { Text = step.Tool, Foreground = new Microsoft.UI.Xaml.Media.SolidColorBrush(Colors.WhiteSmoke), FontSize = 12 });
            row.Children.Add(new TextBlock { Text = step.Status, Foreground = new Microsoft.UI.Xaml.Media.SolidColorBrush(Colors.Gray), FontSize = 10, VerticalAlignment = VerticalAlignment.Center });

            var card = new Border
            {
                Background = new Microsoft.UI.Xaml.Media.SolidColorBrush(Color.FromArgb(15, 255, 255, 255)),
                CornerRadius = new CornerRadius(8),
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
                Foreground = new Microsoft.UI.Xaml.Media.SolidColorBrush(Colors.LightGray),
                FontSize = 11,
            });
            if (!string.IsNullOrEmpty(chunk.SymbolName))
            {
                panel.Children.Add(new TextBlock
                {
                    Text = chunk.SymbolName,
                    Foreground = new Microsoft.UI.Xaml.Media.SolidColorBrush(Colors.Gray),
                    FontSize = 10,
                });
            }
            RetrievedChunksList.Items.Add(panel);
        }

        if (retrieval.ChunksDropped.Count > 0)
        {
            RetrievedChunksList.Items.Add(new TextBlock
            {
                Text = $"{retrieval.ChunksDropped.Count} dropped (budget)",
                Foreground = new Microsoft.UI.Xaml.Media.SolidColorBrush(Colors.OrangeRed),
                FontSize = 10,
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
        RecentSessionsHeader.Visibility = listVisibility;
        SessionsList.Visibility = listVisibility;
    }

    /// <summary>Light dismiss: any press outside the card (or its button) closes it. Registered
    /// with handledEventsToo so presses swallowed by controls still count.</summary>
    private void RootGrid_PointerPressedForDropdown(object sender, Microsoft.UI.Xaml.Input.PointerRoutedEventArgs e)
    {
        if (WorkspaceDropdown.Visibility != Visibility.Visible) return;
        if (e.OriginalSource is DependencyObject source && (IsInside(source, WorkspaceDropdown) || IsInside(source, WorkspaceButton))) return;
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

    private static readonly Windows.UI.Color SessionRestTint = Windows.UI.Color.FromArgb(255, 20, 23, 33);
    private static readonly Windows.UI.Color SessionActiveTint = Windows.UI.Color.FromArgb(255, 8, 145, 178);

    private void SessionRow_Loaded(object sender, RoutedEventArgs e)
    {
        if (sender is Jarvis_Glass.GlassSlab { Tag: Session session } row) ApplySessionHighlight(row, session);
    }

    /// <summary>Tints the active session's row accent, the rest neutral. Called whenever the
    /// active session or the list changes; rows realised later pick it up in SessionRow_Loaded.</summary>
    private void UpdateSessionHighlight()
    {
        foreach (var item in SessionsViewModel.Sessions)
        {
            if (SessionsList.ContainerFromItem(item) is ListViewItem container
                && container.ContentTemplateRoot is Jarvis_Glass.GlassSlab row)
            {
                ApplySessionHighlight(row, item);
            }
        }
    }

    private void ApplySessionHighlight(Jarvis_Glass.GlassSlab row, Session session)
    {
        var active = session.SessionId == ChatViewModel.ActiveSessionId;
        row.TintColor = active ? SessionActiveTint : SessionRestTint;
        row.TintAmount = active ? 0.6 : 0.14;
    }

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
        pane.CloseRequested += () =>
        {
            SettingsHost.Children.Clear();
            SettingsHost.Visibility = Visibility.Collapsed;
        };
        SettingsHost.Children.Add(pane);
        SettingsHost.Visibility = Visibility.Visible;
    }

    private void NewWorkspace_Click(object sender, RoutedEventArgs e)
    {
        SetWorkspaceDropdownOpen(false);
        if (SettingsHost.Children.Count > 0) return;
        var pane = new NewWorkspacePane(ProjectsViewModel, WinRT.Interop.WindowNative.GetWindowHandle(this));
        pane.CloseRequested += () =>
        {
            SettingsHost.Children.Clear();
            SettingsHost.Visibility = Visibility.Collapsed;
        };
        // Nothing else to wire: the pane activates the new project, and ActiveProjectChanged
        // rescopes the chat and rebuilds the dropdown's rows.
        SettingsHost.Children.Add(pane);
        SettingsHost.Visibility = Visibility.Visible;
    }

    private void HudButton_Click(object sender, RoutedEventArgs e) => Hud?.ToggleVisible();

    /// <summary>Set while the switch is being synced FROM the view model, so the Toggled it
    /// raises doesn't start/stop the mic a second time.</summary>
    private bool _syncingMicToggle;

    private async void MicToggle_Toggled(object sender, RoutedEventArgs e)
    {
        if (_syncingMicToggle) return;
        if (MicToggle.IsOn && !VoiceViewModel.IsActive)
        {
            await VoiceViewModel.StartAsync();
        }
        else if (!MicToggle.IsOn && VoiceViewModel.IsActive)
        {
            VoiceViewModel.Stop();
        }
        UpdateVoiceStateText();
    }

    private void UpdateVoiceStateText()
    {
        if (!VoiceViewModel.IsSupported && !string.IsNullOrEmpty(VoiceViewModel.ErrorMessage))
        {
            VoiceStateText.Text = "Mic unavailable";
            MicToggle.IsEnabled = false;
            return;
        }

        _syncingMicToggle = true;
        MicToggle.IsOn = VoiceViewModel.IsActive;
        _syncingMicToggle = false;
        VoiceStateText.Text = VoiceViewModel.IsActive ? VoiceViewModel.State switch
        {
            Jarvis.Core.Models.VoiceState.Listening => "Listening",
            Jarvis.Core.Models.VoiceState.Armed => "Go ahead",
            Jarvis.Core.Models.VoiceState.Thinking => "Working",
            Jarvis.Core.Models.VoiceState.Speaking => "Speaking",
            _ => "Voice off",
        } : "";
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
                    uploaded.Add(await _api.UploadAttachmentAsync(toUpload, ChatViewModel.ActiveSessionId, ChatViewModel.ProjectId));
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
            ComposerBox.Placeholder = "Message Jarvis...";
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
            Background = new Microsoft.UI.Xaml.Media.SolidColorBrush(Color.FromArgb(30, 6, 182, 212)),
            CornerRadius = new CornerRadius(12),
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
                    CornerRadius = new CornerRadius(8),
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
        row.Children.Add(new TextBlock { Text = name, FontSize = 11, Foreground = new Microsoft.UI.Xaml.Media.SolidColorBrush(Colors.White), VerticalAlignment = VerticalAlignment.Center });
        var removeButton = new Button { Content = "✕", FontSize = 9, Padding = new Thickness(4), Background = new Microsoft.UI.Xaml.Media.SolidColorBrush(Colors.Transparent), BorderThickness = new Thickness(0) };
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
                text = $"🖼 {seeingModel} will see the image directly.";
            else if (catalog.Captioner?.Available == true)
                text = "🖼 The current model cannot see images; the local captioner will describe them for it.";
            else
                text = "⚠ The current model cannot see images and no captioner is installed — they will be attached as files only.";
        }
        catch
        {
            text = "🖼 Image attached.";
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
