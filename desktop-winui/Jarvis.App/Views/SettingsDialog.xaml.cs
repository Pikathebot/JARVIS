using Jarvis.Core.Api;
using Jarvis.Core.Models;
using Jarvis_App.Services;
using Jarvis_App.ViewModels;
using Jarvis_Glass;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;

namespace Jarvis_App.Views;

/// <summary>Native port of SettingsDialog.tsx (backend/model info, persona, proactive-actions
/// toggle, plus a Glass quality control that doesn't exist in the original — see the plan's
/// auto-degrade decision).</summary>
public sealed partial class SettingsDialog : ContentDialog
{
    private readonly JarvisApiClient _api;
    private readonly GovernorViewModel _governor;

    public PersonaViewModel PersonaViewModel { get; }
    public RoutinesViewModel RoutinesViewModel { get; }
    public ModelsViewModel ModelsViewModel { get; }

    public SettingsDialog(
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
        Loaded += SettingsDialog_Loaded;
    }

    private async void SettingsDialog_Loaded(object sender, RoutedEventArgs e)
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
                subtitle.Text = $"{model.SizeDisplay}{family} · {model.Directory}";
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
