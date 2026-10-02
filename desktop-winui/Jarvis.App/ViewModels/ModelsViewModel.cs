using System.Collections.ObjectModel;
using CommunityToolkit.Mvvm.ComponentModel;
using Jarvis.Core.Api;
using Jarvis.Core.Models;
using Microsoft.UI.Dispatching;

namespace Jarvis_App.ViewModels;

/// <summary>
/// Backs the model picker in Settings. Wraps GET /api/models and POST /api/models/select.
///
/// Two slots are exposed, not one: "main" is the model chat runs on, "fast" is the smaller one
/// the router falls back to. They are chosen independently, so both can be pointed at whichever
/// local GGUF suits — that is the whole point of having a catalogue rather than a fixed pair of
/// paths in .env.
/// </summary>
public partial class ModelsViewModel : ObservableObject
{
    private readonly JarvisApiClient _api;
    private readonly DispatcherQueue _dispatcher;

    /// <summary>All local models, in the backend's order: recommended (top-level) first.</summary>
    public ObservableCollection<ModelInfo> Models { get; } = new();

    [ObservableProperty]
    public partial string? MainSelection { get; set; }

    [ObservableProperty]
    public partial string? FastSelection { get; set; }

    [ObservableProperty]
    public partial bool IsBusy { get; set; }

    /// <summary>Surfaced verbatim in the dialog — a model that will not load (VRAM, a bad file)
    /// is the expected failure here and the user needs to see why.</summary>
    [ObservableProperty]
    public partial string? StatusMessage { get; set; }

    /// <summary>Getting new models from HuggingFace. Lives here, not in the Settings sheet, so
    /// a download is still watched after the sheet closes and lands in <see cref="Models"/>.</summary>
    public ModelHubViewModel Hub { get; }

    public ModelsViewModel(JarvisApiClient api, DispatcherQueue dispatcher)
    {
        _api = api;
        _dispatcher = dispatcher;
        Hub = new ModelHubViewModel(api, dispatcher, RefreshAsync);
    }

    public async Task RefreshAsync()
    {
        try
        {
            var catalog = await _api.FetchModelCatalogAsync().ConfigureAwait(false);
            _dispatcher.TryEnqueue(() => Apply(catalog));
        }
        catch (Exception ex)
        {
            _dispatcher.TryEnqueue(() => StatusMessage = $"Could not list models: {ex.Message}");
        }
    }

    private void Apply(ModelCatalogResponse catalog)
    {
        Models.Clear();
        foreach (var model in catalog.Models)
        {
            Models.Add(model);
        }

        catalog.Selection.TryGetValue("main", out var main);
        catalog.Selection.TryGetValue("fast", out var fast);
        MainSelection = main;
        FastSelection = fast;
    }

    /// <summary>
    /// Assigns a model to a slot and loads it. Loading takes as long as llama-server needs to
    /// bring the weights onto the GPU, so IsBusy gates the UI for the duration.
    /// </summary>
    public async Task SelectAsync(string slot, ModelInfo model, bool activate = true)
    {
        _dispatcher.TryEnqueue(() =>
        {
            IsBusy = true;
            StatusMessage = activate ? $"Loading {model.Name}…" : null;
        });

        try
        {
            var result = await _api.SelectModelAsync(slot, model.Id, activate).ConfigureAwait(false);
            _dispatcher.TryEnqueue(() =>
            {
                result.Selection.TryGetValue("main", out var main);
                result.Selection.TryGetValue("fast", out var fast);
                MainSelection = main;
                FastSelection = fast;

                StatusMessage = result.Error is not null
                    // The choice is kept regardless, so say so rather than implying it was lost.
                    ? $"{model.Name} is set as {slot}, but did not load: {result.Error}"
                    : activate
                        ? $"{model.Name} loaded as {slot}."
                        : $"{model.Name} set as {slot}; it will load next time that slot is used.";
            });

            await RefreshAsync().ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            _dispatcher.TryEnqueue(() => StatusMessage = $"Could not select {model.Name}: {ex.Message}");
        }
        finally
        {
            _dispatcher.TryEnqueue(() => IsBusy = false);
        }
    }

    /// <summary>Whether a model is the one assigned to a given slot — drives the row badges.</summary>
    public bool IsSelectedFor(string slot, ModelInfo model) =>
        (slot == "main" ? MainSelection : FastSelection) == model.Id;
}
