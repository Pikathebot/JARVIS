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
    public GlassQualityService GlassQuality { get; }

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

        // Still constructed for the Settings dialog's quality/live-capture rows, but no longer
        // registered against the tree: the panels are GlassSlabs on the GlassHost now, not
        // Win2D GlassPanels for it to govern.
        GlassQuality = new GlassQualityService(AwarenessViewModel, GovernorViewModel);

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
            _ = SessionsViewModel.RefreshAsync();
        };
        ChatViewModel.MessageCompleted += completedMessage => DispatcherQueue.TryEnqueue(() => _ = SessionsViewModel.RefreshAsync());

        _ = ProjectsViewModel.RefreshAsync();
        _ = SessionsViewModel.RefreshAsync();

        ExtendTitleBar();
        // Liquid glass behind this window: normal z-band, and it must stay a taskbar window.
        Glass = new Jarvis_Glass.GlassHost(this, topmost: false, showInTaskbar: true);

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

    private async void GovernorPause_Click(object sender, RoutedEventArgs e) =>
        await GovernorViewModel.PauseAsync("User requested manual pause");

    private async void GovernorResume_Click(object sender, RoutedEventArgs e) =>
        await GovernorViewModel.ResumeAsync();

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

    private async void ProjectSwitcher_SelectionChanged(object sender, Microsoft.UI.Xaml.Controls.SelectionChangedEventArgs e)
    {
        if (ProjectSwitcher.SelectedItem is Project project && project.Id != ProjectsViewModel.ActiveProject?.Id)
        {
            await ProjectsViewModel.ActivateAsync(project);
        }
    }

    private async void SessionItem_Tapped(object sender, Microsoft.UI.Xaml.Input.TappedRoutedEventArgs e)
    {
        if (sender is FrameworkElement { Tag: Session session })
        {
            await SessionsViewModel.SelectAsync(session);
        }
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

    private async void Settings_Click(object sender, RoutedEventArgs e)
    {
        var dialog = new SettingsDialog(_api, GovernorViewModel, PersonaViewModel, RoutinesViewModel, ModelsViewModel, GlassQuality)
        {
            XamlRoot = Content.XamlRoot,
        };
        await dialog.ShowAsync();
    }

    private void HudButton_Click(object sender, RoutedEventArgs e) => Hud?.ToggleVisible();

    private async void MicButton_Click(object sender, RoutedEventArgs e)
    {
        if (VoiceViewModel.IsActive)
        {
            VoiceViewModel.Stop();
        }
        else
        {
            await VoiceViewModel.StartAsync();
        }
        UpdateVoiceStateText();
    }

    private void UpdateVoiceStateText()
    {
        if (!VoiceViewModel.IsSupported && !string.IsNullOrEmpty(VoiceViewModel.ErrorMessage))
        {
            VoiceStateText.Text = "Mic unavailable";
            MicButton.Content = "🚫";
            return;
        }

        MicButton.Content = VoiceViewModel.IsActive ? "🔴" : "🎙";
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
            ComposerBox.PlaceholderText = "Uploading attachments…";
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
            ComposerBox.PlaceholderText = "Message Jarvis...";
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
