using Jarvis.Core.Api;
using Jarvis.Core.Models;
using Jarvis_App.Services;
using Jarvis_App.ViewModels;
using Jarvis_Glass;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;

namespace Jarvis_App.Views;

/// <summary>Native port of SettingsDialog.tsx (backend/model info, persona, proactive-actions
/// toggle, routines) as an in-window glass sheet -- see the remark in SettingsPane.xaml for why
/// it is not a ContentDialog. The host shows it in an overlay grid and removes it on
/// <see cref="CloseRequested"/>.</summary>
public sealed partial class SettingsPane : UserControl
{
    public event Action? CloseRequested;

    /// <summary>The sheet itself, apart from its scrim: what rises on open (MainWindow.OpenSheet).</summary>
    public UIElement SheetSurface => Sheet;

    private void Close_Click(object sender, RoutedEventArgs e) => CloseRequested?.Invoke();

    private void Scrim_Tapped(object sender, Microsoft.UI.Xaml.Input.TappedRoutedEventArgs e) => CloseRequested?.Invoke();

    /// <summary>Taps inside the sheet must not reach the scrim's close handler.</summary>
    private void Sheet_Tapped(object sender, Microsoft.UI.Xaml.Input.TappedRoutedEventArgs e) => e.Handled = true;

    private readonly JarvisApiClient _api;
    private readonly GovernorViewModel _governor;

    public PersonaViewModel PersonaViewModel { get; }
    public RoutinesViewModel RoutinesViewModel { get; }
    public ModelsViewModel ModelsViewModel { get; }

    public SettingsPane(
        JarvisApiClient api,
        GovernorViewModel governor,
        PersonaViewModel personaViewModel,
        RoutinesViewModel routinesViewModel,
        ModelsViewModel modelsViewModel)
    {
        _api = api;
        _governor = governor;
        PersonaViewModel = personaViewModel;
        RoutinesViewModel = routinesViewModel;
        ModelsViewModel = modelsViewModel;
        ModelsViewModel.PropertyChanged += (_, _) => RefreshModelState();
        InitializeComponent();
        ModelsViewModel.Hub.PropertyChanged += Hub_PropertyChanged;
        Unloaded += (_, _) => ModelsViewModel.Hub.PropertyChanged -= Hub_PropertyChanged;
        // Not in XAML: a bare integer there is what let the sheet sit on the same
        // layer as the controls behind it, which it could neither cover nor frost.
        Sheet.Layer = GlassLayers.Sheet;
        _syncingAppearance = true;
        AppearanceSwitch.SelectedIndex = (int)Themes.JarvisTheme.Appearance;
        _syncingAppearance = false;
        Loaded += SettingsPane_Loaded;
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
    }

    private async void SettingsPane_Loaded(object sender, RoutedEventArgs e)
    {
        BackendText.Text = string.IsNullOrEmpty(_governor.ActiveBackend) ? "Offline" : _governor.ActiveBackend;
        ModelText.Text = _governor.ConfiguredModel;

        await PersonaViewModel.RefreshAsync();
        RefreshPersonaHighlight();
        AddressTermBox.PlaceholderText = PersonaViewModel.Status?.Active.AddressTerm ?? "sir";

        await RoutinesViewModel.RefreshAsync();

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
                var fit = SlotFit.Line(model.Fit);
                subtitle.Text = $"{model.SizeDisplay}{family} · {model.Directory}" + (fit.Length > 0 ? $"\n{fit}" : "");
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

    // --- Get models ------------------------------------------------------------------------

    private void Hub_PropertyChanged(object? sender, System.ComponentModel.PropertyChangedEventArgs e) => RefreshHubState();

    /// <summary>Busy ring, budget line, status and the repo panel. In code-behind for the same
    /// reason as the model rows: bool/null-to-Visibility hops need converters under x:Bind.</summary>
    private void RefreshHubState()
    {
        var hub = ModelsViewModel.Hub;
        HubBusyRing.IsActive = hub.IsBusy;
        HubBusyRing.Visibility = hub.IsBusy ? Visibility.Visible : Visibility.Collapsed;

        HubBudgetText.Text = hub.BudgetText ?? "";
        HubBudgetText.Visibility = string.IsNullOrEmpty(hub.BudgetText) ? Visibility.Collapsed : Visibility.Visible;

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

    // --- Developer: glass tuning ---------------------------------------------------------------

    private bool _syncingTuning;

    private void DeveloperToggle_Toggled(object sender, RoutedEventArgs e)
    {
        TuningPanel.Visibility = DeveloperToggle.IsOn ? Visibility.Visible : Visibility.Collapsed;
        if (DeveloperToggle.IsOn) BuildTuningRows();
        else TuningRows.Children.Clear(); // unloads the sliders, taking their glass with them
    }

    private void TuningGroups_SelectionChanged(object sender, RoutedEventArgs e)
    {
        if (DeveloperToggle.IsOn) BuildTuningRows();
    }

    private string CurrentTuningGroup => GlassTuning.Groups[Math.Clamp(TuningGroups.SelectedIndex, 0, GlassTuning.Groups.Length - 1)];

    /// <summary>One label/value line and one glass slider per knob in the selected group.
    /// Rebuilt on group change rather than collapsed, so only this group's sliders hold glass.</summary>
    private void BuildTuningRows()
    {
        TuningRows.Children.Clear();
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
}
