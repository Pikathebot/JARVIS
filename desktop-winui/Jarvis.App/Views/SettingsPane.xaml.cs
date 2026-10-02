using Jarvis.Core.Api;
using Jarvis.Core.Models;
using Jarvis_App.Services;
using Jarvis_App.ViewModels;
using Jarvis_Glass;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;

namespace Jarvis_App.Views;

/// <summary>
/// The Settings window's content (PLAN 4.8b2): a section list and one page at a time --
/// General, Models, Persona, Voice, Routines, Developer, About. Hosted by
/// <see cref="SettingsWindow"/>; the pages hold what the old in-window sheet had plus the
/// Models budget card, the Voice listening sliders and the Developer preview.
/// </summary>
public sealed partial class SettingsPane : UserControl
{
    public static readonly string[] Pages = { "General", "Models", "Persona", "Voice", "Routines", "Developer", "About" };

    private readonly JarvisApiClient _api;
    private readonly GovernorViewModel _governor;
    private readonly VoiceViewModel _voice;
    private string _page = "General";

    public PersonaViewModel PersonaViewModel { get; }
    public RoutinesViewModel RoutinesViewModel { get; }
    public ModelsViewModel ModelsViewModel { get; }

    public SettingsPane(
        JarvisApiClient api,
        GovernorViewModel governor,
        PersonaViewModel personaViewModel,
        RoutinesViewModel routinesViewModel,
        ModelsViewModel modelsViewModel,
        VoiceViewModel voice)
    {
        _api = api;
        _governor = governor;
        _voice = voice;
        PersonaViewModel = personaViewModel;
        RoutinesViewModel = routinesViewModel;
        ModelsViewModel = modelsViewModel;
        InitializeComponent();

        ModelsViewModel.PropertyChanged += Models_PropertyChanged;
        ModelsViewModel.Hub.PropertyChanged += Hub_PropertyChanged;
        RoutinesViewModel.Routines.CollectionChanged += Routines_CollectionChanged;
        Unloaded += (_, _) => Detach();

        _syncingAppearance = true;
        AppearanceSwitch.SelectedIndex = (int)Themes.JarvisTheme.Appearance;
        _syncingAppearance = false;
        Loaded += SettingsPane_Loaded;
        ShowPage(_page);
    }

    /// <summary>Stops listening to the view models, which outlive the window (Unloaded is not
    /// reliably raised when a window closes, so the window calls this from Closed too).</summary>
    public void Detach()
    {
        ModelsViewModel.PropertyChanged -= Models_PropertyChanged;
        ModelsViewModel.Hub.PropertyChanged -= Hub_PropertyChanged;
        RoutinesViewModel.Routines.CollectionChanged -= Routines_CollectionChanged;
    }

    /// <summary>Hooks the traffic lights and the drag strip to the hosting window.</summary>
    public void AttachWindow(Window window, AppWindow appWindow) => WindowButtons.Attach(window, appWindow, RootGrid);

    // --- sections ------------------------------------------------------------------------------

    private void Nav_Click(object sender, RoutedEventArgs e)
    {
        if (sender is FrameworkElement { Tag: string page }) ShowPage(page);
    }

    public void ShowPage(string page)
    {
        if (!Pages.Contains(page)) page = "General";
        _page = page;
        PageTitle.Text = page;

        var panels = new (string Name, FrameworkElement Panel, Button Nav)[]
        {
            ("General", GeneralPage, NavGeneral), ("Models", ModelsPage, NavModels), ("Persona", PersonaPage, NavPersona),
            ("Voice", VoicePage, NavVoice), ("Routines", RoutinesPage, NavRoutines),
            ("Developer", DeveloperPage, NavDeveloper), ("About", AboutPage, NavAbout),
        };
        foreach (var (name, panel, nav) in panels)
        {
            var on = name == page;
            panel.Visibility = on ? Visibility.Visible : Visibility.Collapsed;
            nav.Background = on ? Themes.JarvisTheme.Brush("FillSecondary") : new Microsoft.UI.Xaml.Media.SolidColorBrush(Microsoft.UI.Colors.Transparent);
            if (nav.Content is StackPanel content && content.Children.Count > 0 && content.Children[0] is FontIcon icon)
                icon.Foreground = Themes.JarvisTheme.Brush(on ? "Accent" : "LabelSecondary");
        }
        PageScroll.ChangeView(null, 0, null, disableAnimation: true);

        // The Developer page's sliders exist only while it shows (each is a live glass shape).
        if (page == "Developer") BuildTuningRows();
        else TuningRows.Children.Clear();
        if (page == "Models") RefreshBudget();
    }

    private bool _syncingAppearance;

    /// <summary>Dark | Light | High contrast, in <see cref="JarvisAppearance"/> order. Windows'
    /// own high-contrast mode wins (WinUI's controls follow it regardless), so the switch
    /// snaps back and says why.</summary>
    private void AppearanceSwitch_SelectionChanged(object sender, RoutedEventArgs e)
    {
        if (_syncingAppearance) return;
        var chosen = (JarvisAppearance)Math.Clamp(AppearanceSwitch.SelectedIndex, 0, 2);
        if (Themes.JarvisTheme.SystemHighContrast() && chosen != JarvisAppearance.HighContrast)
        {
            AppearanceNote.Visibility = Visibility.Visible;
            _syncingAppearance = true;
            AppearanceSwitch.SelectedIndex = (int)JarvisAppearance.HighContrast;
            _syncingAppearance = false;
            return;
        }
        Themes.JarvisTheme.Set(chosen);
        ShowPage(_page); // nav colours come from the palette
    }

    private async void SettingsPane_Loaded(object sender, RoutedEventArgs e)
    {
        RefreshAbout();
        ShowListening();

        await PersonaViewModel.RefreshAsync();
        RefreshPersonaHighlight();
        RefreshVoice();
        AddressTermBox.PlaceholderText = PersonaViewModel.Status?.Active.AddressTerm ?? "sir";

        await RoutinesViewModel.RefreshAsync();
        Routines_CollectionChanged(null, null);

        await ModelsViewModel.RefreshAsync();
        ModelList.Loaded += (_, _) => RefreshModelRows();
        ModelList.ContainerContentChanging += (_, _) => RefreshModelRows();
        RefreshModelState();

        RefreshHubState();
        _ = ModelsViewModel.Hub.LoadCuratedAsync();

        try
        {
            var awareness = await _api.FetchAwarenessStatusAsync();
            ProactiveActionsToggle.IsOn = awareness.Monitor.ActionsEnabled;
        }
        catch
        {
            // leave default
        }
    }

    // --- Models --------------------------------------------------------------------------------

    private void Models_PropertyChanged(object? sender, System.ComponentModel.PropertyChangedEventArgs e) =>
        DispatcherQueue.TryEnqueue(RefreshModelState);

    /// <summary>
    /// Per-row badges and slot buttons. Done here rather than through x:Bind because "is this
    /// model the one assigned to a slot" is a comparison against view-model state, not a property
    /// of the row's own item, and a Recommended badge needs a bool-to-Visibility hop that x:Bind
    /// cannot do without a converter.
    /// </summary>
    private void RefreshModelRows()
    {
        foreach (var item in ModelList.Items)
        {
            if (item is not ModelInfo model) continue;
            if (ModelList.ContainerFromItem(item) is not ListViewItem { ContentTemplateRoot: FrameworkElement root }) continue;

            if (root.FindName("RecommendedBadge") is FrameworkElement badge)
            {
                badge.Visibility = model.Recommended ? Visibility.Visible : Visibility.Collapsed;
            }

            if (root.FindName("ModelSubtitle") is TextBlock subtitle)
            {
                // The containing directory is what distinguishes a top-level model from an
                // identically-named copy inside a vendor download tree.
                var family = model.Family is null ? "" : $" · {model.Family}";
                var vision = model.Projector is null ? " · text only" : " · vision";
                var fit = SlotFit.Line(model.Fit);
                subtitle.Text = $"{model.SizeDisplay}{family}{vision} · {model.Directory}" + (fit.Length > 0 ? $"\n{fit}" : "");
            }

            SetSlotButton(root.FindName("MainButton") as Button, "main", model);
            SetSlotButton(root.FindName("FastButton") as Button, "fast", model);
        }
    }

    private void SetSlotButton(Button? button, string slot, ModelInfo model)
    {
        if (button is null) return;

        var assigned = ModelsViewModel.IsSelectedFor(slot, model);
        button.IsEnabled = !assigned && !ModelsViewModel.IsBusy;
        button.Opacity = assigned ? 1.0 : 0.75;
        button.Content = assigned ? (slot == "main" ? "Main ✓" : "Fast ✓") : (slot == "main" ? "Main" : "Fast");
    }

    private void RefreshModelState()
    {
        ModelBusyRing.IsActive = ModelsViewModel.IsBusy;
        ModelBusyRing.Visibility = ModelsViewModel.IsBusy ? Visibility.Visible : Visibility.Collapsed;

        ModelStatusText.Text = ModelsViewModel.StatusMessage ?? "";
        ModelStatusText.Visibility = string.IsNullOrEmpty(ModelsViewModel.StatusMessage)
            ? Visibility.Collapsed
            : Visibility.Visible;

        RefreshModelRows();
        RefreshBudget();
    }

    /// <summary>The "Free for a model" card: the governor's budget against the card's total.</summary>
    private void RefreshBudget()
    {
        var budget = ModelsViewModel.FitBudgetMb;
        var total = ModelsViewModel.VramTotalMb;
        if (budget is null || total is null || total <= 0)
        {
            BudgetMetric.Text = "—";
            BudgetDetail.Text = "Waiting for a GPU reading from the backend.";
            BudgetOtherColumn.Width = new GridLength(0, GridUnitType.Star);
            BudgetFreeColumn.Width = new GridLength(1, GridUnitType.Star);
            return;
        }
        var other = Math.Max(0, total.Value - budget.Value);
        BudgetMetric.Text = $"{budget.Value / 1024:0.0} GB";
        BudgetOtherColumn.Width = new GridLength(other, GridUnitType.Star);
        BudgetFreeColumn.Width = new GridLength(budget.Value, GridUnitType.Star);
        BudgetDetail.Text = $"{total.Value / 1024:0.0} GB card · {other / 1024:0.0} GB held by other apps (the most in the last 10 minutes). "
            + "Fits leaves 500 MB spare; Spills would run partly from shared memory, slowly.";
    }

    private async void SelectMainModel_Click(object sender, RoutedEventArgs e) => await SelectModelAsync(sender, "main");

    private async void SelectFastModel_Click(object sender, RoutedEventArgs e) => await SelectModelAsync(sender, "fast");

    private async Task SelectModelAsync(object sender, string slot)
    {
        if (sender is FrameworkElement { Tag: ModelInfo model })
        {
            // Persist the choice only -- do not restart llama-server here. Loading the weights
            // happens lazily on the next chat send (LlamaCppProvider.ensure_running), so picking
            // a model in Settings shouldn't itself trigger a load/unload cycle.
            await ModelsViewModel.SelectAsync(slot, model, activate: false);
        }
    }

    // --- Get models ----------------------------------------------------------------------------

    private void Hub_PropertyChanged(object? sender, System.ComponentModel.PropertyChangedEventArgs e) =>
        DispatcherQueue.TryEnqueue(RefreshHubState);

    /// <summary>Busy ring, status and the repo panel. In code-behind for the same reason as the
    /// model rows: bool/null-to-Visibility hops need converters under x:Bind.</summary>
    private void RefreshHubState()
    {
        var hub = ModelsViewModel.Hub;
        HubBusyRing.IsActive = hub.IsBusy;
        HubBusyRing.Visibility = hub.IsBusy ? Visibility.Visible : Visibility.Collapsed;

        HubStatusText.Text = hub.StatusMessage ?? "";
        HubStatusText.Visibility = string.IsNullOrEmpty(hub.StatusMessage) ? Visibility.Collapsed : Visibility.Visible;

        RepoPanel.Visibility = hub.OpenRepo is null ? Visibility.Collapsed : Visibility.Visible;
        RepoTitle.Text = hub.OpenRepo ?? "";
        RepoVisionRow.Visibility = hub.RepoHasVision ? Visibility.Visible : Visibility.Collapsed;
        if (RepoVisionToggle.IsOn != hub.IncludeVision) RepoVisionToggle.IsOn = hub.IncludeVision;
    }

    private async void DownloadCurated_Click(object sender, RoutedEventArgs e)
    {
        if (sender is FrameworkElement { Tag: CuratedModel model }) await ModelsViewModel.Hub.DownloadCuratedAsync(model);
    }

    private async void HubSearch_Click(object sender, RoutedEventArgs e) => await ModelsViewModel.Hub.SearchAsync(HubSearchBox.Text);

    private async void HubSearchBox_KeyDown(object sender, Microsoft.UI.Xaml.Input.KeyRoutedEventArgs e)
    {
        if (e.Key != Windows.System.VirtualKey.Enter) return;
        e.Handled = true;
        await ModelsViewModel.Hub.SearchAsync(HubSearchBox.Text);
    }

    private async void OpenRepo_Click(object sender, RoutedEventArgs e)
    {
        if (sender is FrameworkElement { Tag: HubSearchResult result }) await ModelsViewModel.Hub.OpenRepoAsync(result.Repo);
    }

    private void CloseRepo_Click(object sender, RoutedEventArgs e) => ModelsViewModel.Hub.CloseRepo();

    private void RepoVisionToggle_Toggled(object sender, RoutedEventArgs e)
    {
        if (ModelsViewModel.Hub.IncludeVision != RepoVisionToggle.IsOn) ModelsViewModel.Hub.IncludeVision = RepoVisionToggle.IsOn;
    }

    private async void DownloadRepoFile_Click(object sender, RoutedEventArgs e)
    {
        if (sender is FrameworkElement { Tag: HubFileInfo file }) await ModelsViewModel.Hub.DownloadRepoFileAsync(file);
    }

    private async void CancelDownload_Click(object sender, RoutedEventArgs e)
    {
        if (sender is FrameworkElement { Tag: DownloadItem item }) await ModelsViewModel.Hub.CancelAsync(item);
    }

    // --- Persona -------------------------------------------------------------------------------

    private void RefreshPersonaHighlight()
    {
        var activeId = PersonaViewModel.Status?.ActiveId;
        foreach (var item in PersonaList.Items)
        {
            var container = PersonaList.ContainerFromItem(item) as ListViewItem;
            if (container?.ContentTemplateRoot is not FrameworkElement root) continue;
            var badge = root.FindName("ActiveBadge") as FrameworkElement;
            if (badge is not null && item is PersonaSummary summary)
            {
                badge.Visibility = summary.Id == activeId ? Visibility.Visible : Visibility.Collapsed;
            }
        }
    }

    private async void PersonaItem_Tapped(object sender, Microsoft.UI.Xaml.Input.TappedRoutedEventArgs e)
    {
        if (sender is FrameworkElement { Tag: PersonaSummary persona })
        {
            await PersonaViewModel.SelectAsync(persona);
            RefreshPersonaHighlight();
            RefreshVoice();
        }
    }

    private async void ApplyAddressTerm_Click(object sender, RoutedEventArgs e)
    {
        var text = AddressTermBox.Text.Trim();
        if (string.IsNullOrEmpty(text)) return;
        await PersonaViewModel.SetAddressTermAsync(text);
        AddressTermBox.Text = "";
        AddressTermBox.PlaceholderText = text;
    }

    private async void ResetOverrides_Click(object sender, RoutedEventArgs e)
    {
        await PersonaViewModel.ResetOverridesAsync();
        AddressTermBox.PlaceholderText = PersonaViewModel.Status?.Active.AddressTerm ?? "sir";
    }

    private async void ProactiveActionsToggle_Toggled(object sender, RoutedEventArgs e)
    {
        try
        {
            await _api.UpdateAwarenessConfigAsync(new AwarenessConfigPatch { ActionsEnabled = ProactiveActionsToggle.IsOn });
        }
        catch
        {
            // best-effort; toggle stays as the user left it visually
        }
    }

    // --- Voice ---------------------------------------------------------------------------------

    private void RefreshVoice()
    {
        var active = PersonaViewModel.Status?.Active;
        VoiceIdText.Text = string.IsNullOrEmpty(active?.VoiceId) ? "—" : active!.VoiceId;
        var persona = PersonaViewModel.Available.FirstOrDefault(p => p.Id == PersonaViewModel.Status?.ActiveId);
        VoiceSource.Text = persona is null
            ? "Kokoro, on the CPU. Set by the active persona."
            : $"Kokoro, on the CPU. Set by the active persona ({persona.DisplayName}).";
    }

    private async void PlaySample_Click(object sender, RoutedEventArgs e)
    {
        PlaySampleButton.IsEnabled = false;
        try
        {
            var voiceId = PersonaViewModel.Status?.Active.VoiceId;
            await _voice.SpeakAsync("This is how I sound.", string.IsNullOrEmpty(voiceId) ? null : voiceId);
        }
        finally
        {
            PlaySampleButton.IsEnabled = true;
        }
    }

    private bool _syncingListening;

    /// <summary>Puts the saved values on the two sliders (GlassSlider runs 0..1).</summary>
    private void ShowListening()
    {
        _syncingListening = true;
        SpeechRatioSlider.Value = (VoiceSettings.SpeechRatio - VoiceSettings.MinSpeechRatio) / (VoiceSettings.MaxSpeechRatio - VoiceSettings.MinSpeechRatio);
        PauseSlider.Value = (double)(VoiceSettings.PauseMs - VoiceSettings.MinPauseMs) / (VoiceSettings.MaxPauseMs - VoiceSettings.MinPauseMs);
        _syncingListening = false;
        SpeechRatioText.Text = $"{VoiceSettings.SpeechRatio:0.0}× the room";
        PauseText.Text = $"{VoiceSettings.PauseMs} ms";
    }

    private void SpeechRatioSlider_ValueChanged(object sender, RoutedEventArgs e)
    {
        if (_syncingListening) return;
        VoiceSettings.SetSpeechRatio(VoiceSettings.MinSpeechRatio + (float)SpeechRatioSlider.Value * (VoiceSettings.MaxSpeechRatio - VoiceSettings.MinSpeechRatio));
        SpeechRatioText.Text = $"{VoiceSettings.SpeechRatio:0.0}× the room";
    }

    private void PauseSlider_ValueChanged(object sender, RoutedEventArgs e)
    {
        if (_syncingListening) return;
        // 50 ms steps: finer than anyone can tell apart, coarse enough to read.
        var ms = VoiceSettings.MinPauseMs + PauseSlider.Value * (VoiceSettings.MaxPauseMs - VoiceSettings.MinPauseMs);
        VoiceSettings.SetPauseMs((int)(Math.Round(ms / 50) * 50));
        PauseText.Text = $"{VoiceSettings.PauseMs} ms";
    }

    private void ResetListening_Click(object sender, RoutedEventArgs e)
    {
        VoiceSettings.SetSpeechRatio(VoiceSettings.DefaultSpeechRatio);
        VoiceSettings.SetPauseMs(VoiceSettings.DefaultPauseMs);
        ShowListening();
    }

    // --- Routines ------------------------------------------------------------------------------

    private void Routines_CollectionChanged(object? sender, System.Collections.Specialized.NotifyCollectionChangedEventArgs? e) =>
        RoutinesEmpty.Visibility = RoutinesViewModel.Routines.Count == 0 ? Visibility.Visible : Visibility.Collapsed;

    private async void AddRoutine_Click(object sender, RoutedEventArgs e)
    {
        var name = RoutineNameBox.Text.Trim();
        if (string.IsNullOrEmpty(name)) return;
        var time = RoutineTimePicker.Time;

        var kind = (RoutineKindCombo.SelectedItem as ComboBoxItem)?.Content as string == "message"
            ? RoutineKind.Message
            : RoutineKind.Briefing;

        await RoutinesViewModel.CreateAsync(name, $"{time.Hours:D2}:{time.Minutes:D2}", kind, RoutineMessageBox.Text);
        RoutineNameBox.Text = "";
        RoutineMessageBox.Text = "";
    }

    private async void RunRoutine_Click(object sender, RoutedEventArgs e)
    {
        if (sender is FrameworkElement { Tag: Routine routine })
        {
            await RoutinesViewModel.RunNowAsync(routine);
        }
    }

    private async void RemoveRoutine_Click(object sender, RoutedEventArgs e)
    {
        if (sender is FrameworkElement { Tag: Routine routine })
        {
            await RoutinesViewModel.DeleteAsync(routine);
        }
    }

    // --- Developer: glass tuning ---------------------------------------------------------------

    private bool _syncingTuning;

    private void TuningGroups_SelectionChanged(object sender, RoutedEventArgs e)
    {
        if (_page == "Developer") BuildTuningRows();
    }

    private string CurrentTuningGroup => GlassTuning.Groups[Math.Clamp(TuningGroups.SelectedIndex, 0, GlassTuning.Groups.Length - 1)];

    /// <summary>The preview shows the controls the selected group tunes; the rest are collapsed,
    /// which also takes their glass off the scene.</summary>
    private void ShowPreview(string group)
    {
        void Show(FrameworkElement e, bool on) => e.Visibility = on ? Visibility.Visible : Visibility.Collapsed;
        Show(PreviewToggles, group is "Lens" or "Toggle");
        Show(PreviewSliders, group is "Lens" or "Slider");
        Show(PreviewButtons, group == "Button");
        Show(PreviewFields, group == "Field");
        Show(PreviewPanels, group == "Panels");
        PreviewSliders.Margin = group == "Lens" ? new Thickness(0, 80, 0, 0) : new Thickness(0);
        PreviewToggles.Margin = group == "Lens" ? new Thickness(0, 0, 0, 40) : new Thickness(0);
        PreviewSlider.Value = 0.35;
        PreviewHint.Text = group switch
        {
            "Panels" => "The panel group scales every glass panel in Jarvis at once.",
            "Field" => "Click into the field to see it focused.",
            _ => "Press and hold a control to see it lifted (or raise Force lift in Lens).",
        };
    }

    /// <summary>One label/value line and one glass slider per knob in the selected group.
    /// Rebuilt on group change rather than collapsed, so only this group's sliders hold glass.</summary>
    private void BuildTuningRows()
    {
        TuningRows.Children.Clear();
        ShowPreview(CurrentTuningGroup);
        var resources = Application.Current.Resources;
        foreach (var knob in GlassTuning.InGroup(CurrentTuningGroup))
        {
            var label = new TextBlock { Text = knob.Label, Style = (Style)resources["FootnoteText"] };
            var value = new TextBlock { Style = (Style)resources["SecondaryFootnoteText"], HorizontalAlignment = HorizontalAlignment.Right };
            var header = new Grid();
            header.Children.Add(label);
            header.Children.Add(value);

            var slider = new GlassSlider { Width = double.NaN, HorizontalAlignment = HorizontalAlignment.Stretch };
            _syncingTuning = true;
            slider.Value = (knob.Get() - knob.Min) / (knob.Max - knob.Min);
            _syncingTuning = false;
            ShowTuningValue(value, knob);
            slider.ValueChanged += (_, _) =>
            {
                if (_syncingTuning) return;
                knob.Set(knob.Min + (float)slider.Value * (knob.Max - knob.Min));
                ShowTuningValue(value, knob);
                GlassTuning.Changed();
                GlassTuningStore.SaveSoon();
            };

            var row = new StackPanel { Spacing = 0 };
            row.Children.Add(header);
            row.Children.Add(slider);
            TuningRows.Children.Add(row);
        }
        TuningStatus.Text = $"{GlassTuning.Changes().Count} value(s) changed from the defaults.";
    }

    private static void ShowTuningValue(TextBlock text, GlassKnob knob)
    {
        var range = knob.Max - knob.Min;
        var format = range <= 1f ? "0.000" : range <= 10f ? "0.00" : "0.0";
        text.Text = GlassTuning.IsChanged(knob)
            ? $"{knob.Get().ToString(format)}  (was {knob.Default.ToString(format)})"
            : knob.Get().ToString(format);
    }

    private void ResetTuningGroup_Click(object sender, RoutedEventArgs e)
    {
        GlassTuning.Reset(CurrentTuningGroup);
        GlassTuningStore.Save();
        BuildTuningRows();
    }

    private void ResetTuningAll_Click(object sender, RoutedEventArgs e)
    {
        GlassTuning.Reset();
        GlassTuningStore.Save();
        BuildTuningRows();
    }

    private void CopyTuning_Click(object sender, RoutedEventArgs e)
    {
        var package = new Windows.ApplicationModel.DataTransfer.DataPackage();
        package.SetText(GlassTuning.Describe());
        Windows.ApplicationModel.DataTransfer.Clipboard.SetContent(package);
        var count = GlassTuning.Changes().Count;
        TuningStatus.Text = count == 0
            ? "Nothing to copy: every value is at its default."
            : $"Copied {count} value(s) as code -- paste them to Claude to bake in.";
    }

    // --- About ---------------------------------------------------------------------------------

    private void RefreshAbout()
    {
        BackendText.Text = string.IsNullOrEmpty(_governor.ActiveBackend) ? "Offline" : $"Running · {_governor.ActiveBackend}";
        ModelText.Text = _governor.ConfiguredModel;
        try
        {
            var v = Windows.ApplicationModel.Package.Current.Id.Version;
            VersionText.Text = $"{v.Major}.{v.Minor}.{v.Build}.{v.Revision}";
        }
        catch
        {
            VersionText.Text = "development build";
        }
    }

    private void OpenRepoFolder_Click(object sender, RoutedEventArgs e)
    {
        if (string.IsNullOrEmpty(App.RepoRoot)) return;
        try
        {
            System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo("explorer.exe", $"\"{App.RepoRoot}\"") { UseShellExecute = true });
        }
        catch (Exception ex)
        {
            App.Log($"open repo folder failed: {ex.Message}");
        }
    }
}
