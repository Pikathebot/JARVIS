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

        RightPanelViewModel.PropertyChanged += (_, args) =>
        {
            if (args.PropertyName == nameof(ViewModels.RightPanelViewModel.IsOpen))
            {
                DispatcherQueue.TryEnqueue(() =>
                {
                    var open = RightPanelViewModel.IsOpen;
                    RightPanelColumn.Width = new GridLength(open ? 368 : 0);
                    RightPanel.Visibility = open ? Visibility.Visible : Visibility.Collapsed;
                });
            }
        };

        SessionsViewModel.SessionSelected += async sessionId => await ChatViewModel.LoadSessionAsync(sessionId);
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

        _ = ProjectsViewModel.RefreshAsync();
        _ = SessionsViewModel.RefreshAsync();

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
            WorkspaceDropdown.Visibility = Visibility.Collapsed;
            return;
        }
        // Float the card just under the button, matching its width.
        var origin = WorkspaceButton.TransformToVisual(RootGrid).TransformPoint(new Windows.Foundation.Point(0, WorkspaceButton.ActualHeight + 6));
        WorkspaceDropdown.Margin = new Thickness(origin.X, origin.Y, 0, 0);
        WorkspaceDropdown.MinWidth = WorkspaceButton.ActualWidth;
        WorkspaceDropdown.Visibility = Visibility.Visible;
        UpdateWorkspaceHighlight();
    }

    /// <summary>Light dismiss: any press outside the card (or its button) closes it. Registered
    /// with handledEventsToo so presses swallowed by controls still count.</summary>
    private void RootGrid_PointerPressedForDropdown(object sender, Microsoft.UI.Xaml.Input.PointerRoutedEventArgs e)
    {
        if (WorkspaceDropdown.Visibility != Visibility.Visible) return;
        if (e.OriginalSource is DependencyObject source && (IsInside(source, WorkspaceDropdown) || IsInside(source, WorkspaceButton))) return;
        WorkspaceDropdown.Visibility = Visibility.Collapsed;
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

    private async void WorkspaceMenu_SelectionChanged(object sender, RoutedEventArgs e)
    {
        if (_syncingWorkspaceMenu) return;
        var index = WorkspaceMenu.SelectedIndex;
        if (index < 0 || index >= ProjectsViewModel.Projects.Count) return;
        var project = ProjectsViewModel.Projects[index];
        // Let the puck land before the card folds away.
        await Task.Delay(180);
        WorkspaceDropdown.Visibility = Visibility.Collapsed;
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
        if (sender is Jarvis_Glass.GlassSlab row) ApplySessionHighlight(row);
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
                ApplySessionHighlight(row);
            }
        }
    }

    private void ApplySessionHighlight(Jarvis_Glass.GlassSlab row)
    {
        var active = row.Tag is Session s && s.SessionId == ChatViewModel.ActiveSessionId;
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
                    uploaded.Add(await _api.UploadAttachmentAsync(path, ChatViewModel.ActiveSessionId, ChatViewModel.ProjectId));
                }
                catch
                {
                    // best-effort — a failed upload just isn't included in the turn
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
        var chip = new Border
        {
            Background = new Microsoft.UI.Xaml.Media.SolidColorBrush(Color.FromArgb(30, 6, 182, 212)),
            CornerRadius = new CornerRadius(12),
            Padding = new Thickness(10, 4, 6, 4),
        };
        var row = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 6 };
        row.Children.Add(new TextBlock { Text = name, FontSize = 11, Foreground = new Microsoft.UI.Xaml.Media.SolidColorBrush(Colors.White), VerticalAlignment = VerticalAlignment.Center });
        var removeButton = new Button { Content = "✕", FontSize = 9, Padding = new Thickness(4), Background = new Microsoft.UI.Xaml.Media.SolidColorBrush(Colors.Transparent), BorderThickness = new Thickness(0) };
        removeButton.Click += (_, _) =>
        {
            _pendingAttachmentPaths.Remove(path);
            AttachmentChips.Items.Remove(chip);
        };
        row.Children.Add(removeButton);
        chip.Child = row;
        AttachmentChips.Items.Add(chip);
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
