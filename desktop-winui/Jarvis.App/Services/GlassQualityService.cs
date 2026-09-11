using System.ComponentModel;
using Jarvis.Core.Models;
using Jarvis_App.ViewModels;
using Jarvis_Glass;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Media;
using Microsoft.Win32;
using Windows.System.Power;

namespace Jarvis_App.Services;

/// <summary>
/// Connects <see cref="GlassQualityController"/>'s policy to the signals it needs and pushes the
/// result onto every <see cref="GlassPanel"/> in a window. The controller has existed since the
/// glass system was scaffolded but nothing ever fed it, so the auto-degrade the plan calls for
/// never actually fired.
///
/// Three signal sources, each the authority on a different thing:
/// <list type="bullet">
/// <item>GPU load — the awareness snapshot's <c>gpu_util_percent</c>, which the backend already
/// streams every poll. No new endpoint, and it is the same number the governor throttles on.</item>
/// <item>Model loaded — the health poll's <c>llama_connected</c>. A loaded model means the 4060's
/// 8GB is already spoken for and the glass should not be competing for it.</item>
/// <item>The OS's own floor — Settings &gt; Personalization &gt; Colors &gt; Transparency effects,
/// and Battery Saver. Windows refuses to render acrylic in either state, so pretending otherwise
/// would leave the shader drawn over a flat backdrop.</item>
/// </list>
///
/// Panels are found by walking the visual tree rather than by name, so a panel added to the XAML
/// later is governed automatically instead of silently staying at Full.
/// </summary>
public sealed class GlassQualityService : IDisposable
{
    private readonly GlassQualityController _controller = new();
    private readonly AwarenessViewModel _awareness;
    private readonly GovernorViewModel _governor;
    private readonly List<FrameworkElement> _roots = new();

    private double _gpuUtilPercent;
    private bool _modelLoaded;

    private const string LiveCaptureSettingKey = "LiveCaptureEnabled";

    public GlassQualityController Controller => _controller;

    /// <summary>The user's Settings toggle, persisted locally (client-only preference, no backend
    /// relevance). Live capture is continuous whole-screen capture -- a real privacy/perf concern
    /// -- so this defaults to off and stays off until the user explicitly opts in.</summary>
    public bool LiveCaptureEnabled { get; private set; }

    public GlassQualityService(AwarenessViewModel awareness, GovernorViewModel governor)
    {
        _awareness = awareness;
        _governor = governor;

        LiveCaptureEnabled = Windows.Storage.ApplicationData.Current.LocalSettings.Values[LiveCaptureSettingKey] is bool stored && stored;

        _awareness.PropertyChanged += OnAwarenessChanged;
        _governor.PropertyChanged += OnGovernorChanged;
        _controller.QualityChanged += ApplyToAllRoots;
    }

    /// <summary>Registers a window's content root; its glass panels follow the controller from now on.</summary>
    public void Register(FrameworkElement root)
    {
        _roots.Add(root);
        Apply(root, _controller.Current);
    }

    /// <summary>Settings' "Glass quality" control writes here; Auto hands control back to the policy.</summary>
    public void SetMode(GlassQualityMode mode)
    {
        _controller.Mode = mode;
        Evaluate();
    }

    /// <summary>Settings' live-capture toggle writes here. Re-applies immediately so panels that
    /// are already at Full tier pick up (or drop) capture without waiting for the next quality
    /// re-evaluation.</summary>
    public void SetLiveCaptureEnabled(bool enabled)
    {
        LiveCaptureEnabled = enabled;
        Windows.Storage.ApplicationData.Current.LocalSettings.Values[LiveCaptureSettingKey] = enabled;
        ApplyToAllRoots(_controller.Current);
    }

    private void OnAwarenessChanged(object? sender, PropertyChangedEventArgs e)
    {
        if (e.PropertyName != nameof(AwarenessViewModel.Snapshot)) return;
        if (_awareness.Snapshot is not AwarenessSnapshot snapshot) return;

        _gpuUtilPercent = snapshot.GpuUtilPercent;
        Evaluate();
    }

    private void OnGovernorChanged(object? sender, PropertyChangedEventArgs e)
    {
        if (e.PropertyName != nameof(GovernorViewModel.LlamaConnected)) return;

        _modelLoaded = _governor.LlamaConnected;
        Evaluate();
    }

    private void Evaluate() =>
        _controller.Evaluate(_gpuUtilPercent, _modelLoaded, TransparencyEffectsEnabled(), BatterySaverActive());

    private void ApplyToAllRoots(GlassQuality quality)
    {
        foreach (var root in _roots)
        {
            Apply(root, quality);
        }
    }

    private void Apply(DependencyObject root, GlassQuality quality)
    {
        foreach (var panel in FindGlassPanels(root))
        {
            panel.Quality = quality;
            panel.LiveCaptureAvailable = LiveCaptureEnabled && quality == GlassQuality.Full;
            // Reuses the same live-capture setting rather than a second toggle -- refraction only
            // has a real backdrop to bend once that's on (or a usable wallpaper file).
            panel.EdgeRefractionAvailable = LiveCaptureEnabled && quality == GlassQuality.Full;
        }
    }

    private static IEnumerable<GlassPanel> FindGlassPanels(DependencyObject root)
    {
        if (root is GlassPanel panel)
        {
            yield return panel;
            // A GlassPanel's own subtree is its content; panels do not nest, so stop here.
            yield break;
        }

        var count = VisualTreeHelper.GetChildrenCount(root);
        for (var i = 0; i < count; i++)
        {
            foreach (var found in FindGlassPanels(VisualTreeHelper.GetChild(root, i)))
            {
                yield return found;
            }
        }
    }

    /// <summary>
    /// Settings &gt; Personalization &gt; Colors &gt; Transparency effects. There is no WinRT API
    /// for this one, so the registry value it writes is the only way to read it; a missing value
    /// means the default, which is on.
    /// </summary>
    private static bool TransparencyEffectsEnabled()
    {
        try
        {
            using var key = Registry.CurrentUser.OpenSubKey(
                @"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize");
            return key?.GetValue("EnableTransparency") is not int value || value != 0;
        }
        catch
        {
            return true;
        }
    }

    private static bool BatterySaverActive()
    {
        try
        {
            return PowerManager.EnergySaverStatus == EnergySaverStatus.On;
        }
        catch
        {
            return false;
        }
    }

    public void Dispose()
    {
        _awareness.PropertyChanged -= OnAwarenessChanged;
        _governor.PropertyChanged -= OnGovernorChanged;
        _controller.QualityChanged -= ApplyToAllRoots;
        _roots.Clear();
    }
}
