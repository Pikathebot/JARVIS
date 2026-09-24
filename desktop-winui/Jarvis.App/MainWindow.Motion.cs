using Jarvis_App.ViewModels;
using Jarvis_Glass;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media.Animation;

namespace Jarvis_App;

/// <summary>
/// The window's motion: the startup sequence, the spatial switch between the two spaces, and
/// sheets rising in. Everything moves through <see cref="GlassTransition"/> -- springs, not
/// eased curves, and only translation and opacity, so nothing re-lays out mid-flight -- which
/// moves and fades each element's glass with its XAML.
///
/// Budget: every glass shape that moves re-renders its layer and every layer above it each
/// frame, and frost is the expensive pass. So the space switch moves only the two lists' item
/// panels (message bubbles and session rows, which sit inside their panels' clip), never the
/// panels; the startup reveal is the one time the panels themselves move.
/// </summary>
public sealed partial class MainWindow
{
    // Springs, as (response s, damping ratio). Entrances land with a whisper of overshoot
    // (0.86, SwiftUI's .snappy territory); exits are critically damped and quick.
    private static readonly GlassTransition.Spring SplashIn = new(0.5, 1.0);
    private static readonly GlassTransition.Spring SplashOut = new(0.32, 1.0);
    private static readonly GlassTransition.Spring RevealIn = new(0.55, 0.86);
    private static readonly GlassTransition.Spring PageOut = new(0.22, 1.0);
    private static readonly GlassTransition.Spring PageIn = new(0.42, 0.86);
    private static readonly GlassTransition.Spring SheetIn = new(0.45, 0.86);
    private static readonly GlassTransition.Spring SheetOut = new(0.26, 1.0);
    private static readonly GlassTransition.Spring Backdrop = new(0.3, 1.0);

    /// <summary>DIPs the panels rise through on reveal.</summary>
    private const double RevealRise = 18;
    /// <summary>DIPs a page slides on a space switch: a nudge toward where the space lives on the
    /// switch (Freeform left, Workspace right), not a full-width carousel.</summary>
    private const double PageShift = 36;
    /// <summary>DIPs a sheet rises through on open.</summary>
    private const double SheetRise = 28;

    private readonly Dictionary<UIElement, GlassTransition> _motions = new();

    /// <summary>One transition per element, so two animations on it retarget one spring instead
    /// of fighting over its Opacity and transform.</summary>
    private GlassTransition Motion(UIElement element, bool affectsGlass = true)
    {
        if (!_motions.TryGetValue(element, out var motion))
        {
            motion = new GlassTransition(element, affectsGlass);
            _motions[element] = motion;
        }
        return motion;
    }

    // ---- startup ------------------------------------------------------------------------------

    private readonly long _launchedAt = System.Diagnostics.Stopwatch.GetTimestamp();
    private Storyboard? _startupBreathing;
    private bool _revealed;
    /// <summary>The session-list fetch the first ActiveProjectChanged started; the reveal waits
    /// for it so the sidebar doesn't fill in after the panels have landed.</summary>
    private Task? _sessionsRefresh;

    /// <summary>Called from the constructor, before anything has published glass: the panels
    /// start invisible and lowered, and the startup card fades up in their place.</summary>
    private void BeginStartup()
    {
        StartupCard.Layer = GlassLayers.Sheet; // over the panels while they fade in beneath it
        BodyGrid.IsHitTestVisible = false;      // invisible is not untouchable in XAML
        Motion(SidebarSlab).Set(0, RevealRise, 0);
        Motion(ChatColumn).Set(0, RevealRise, 0);
        Motion(TitleBarRow, affectsGlass: false).Set(0, 0, 0);
        Motion(StartupCard).Set(0, 10, 0);
        _ = Motion(StartupCard).AnimateTo(0, 0, 1, SplashIn);

        // The caption breathes while the backend boots. A plain XAML storyboard: the caption
        // has no glass, so this runs on the compositor and costs the glass renderer nothing.
        var breathe = new DoubleAnimation
        {
            From = 1.0,
            To = 0.4,
            Duration = new Duration(TimeSpan.FromSeconds(1.4)),
            AutoReverse = true,
            RepeatBehavior = RepeatBehavior.Forever,
            EasingFunction = new SineEase { EasingMode = EasingMode.EaseInOut },
        };
        Storyboard.SetTarget(breathe, StartupStatus);
        Storyboard.SetTargetProperty(breathe, "Opacity");
        _startupBreathing = new Storyboard();
        _startupBreathing.Children.Add(breathe);
        _startupBreathing.Begin();

        // BackendHost gives uvicorn 20 s; past that the app is still usable offline (the
        // governor pill says so), so it opens anyway rather than sit on the card.
        _ = RevealAfterAsync(TimeSpan.FromSeconds(25), "timed out waiting for the backend");
    }

    private async Task RevealAfterAsync(TimeSpan delay, string why)
    {
        await Task.Delay(delay);
        Reveal(why);
    }

    /// <summary>The backend answered: load projects (which scopes and fetches the sessions),
    /// then open the window onto them. Also runs after a backend restart, when it only
    /// refreshes.</summary>
    private async void OnBackendCameOnline()
    {
        if (!_revealed)
        {
            StartupStatus.Text = LoadSavedSpace() == ChatSpace.Freeform ? "Opening Freeform" : "Opening your workspace";
        }
        await ProjectsViewModel.RefreshAsync();
        if (_revealed) return;
        // RefreshAsync returns once the UI update is queued; that update (ActiveProjectChanged)
        // starts the sessions fetch. Capped: a slow list fills in on screen rather than hold the
        // window shut.
        await Task.Yield();
        await Task.WhenAny(_sessionsRefresh ?? Task.CompletedTask, Task.Delay(1500));
        Reveal("state loaded");
    }

    private async void Reveal(string why)
    {
        if (_revealed) return;
        _revealed = true;
        App.Log($"startup: revealing ({why}) after {System.Diagnostics.Stopwatch.GetElapsedTime(_launchedAt).TotalMilliseconds:F0} ms");

        // Let the card finish arriving if the backend beat it (an already-running backend
        // answers in well under its entrance), so the sequence never reads as a flicker.
        var shown = System.Diagnostics.Stopwatch.GetElapsedTime(_launchedAt);
        var minimum = TimeSpan.FromMilliseconds(450);
        if (shown < minimum) await Task.Delay(minimum - shown);

        BodyGrid.IsHitTestVisible = true;
        _ = FadeOutStartupCardAsync();
        _ = Motion(SidebarSlab).AnimateTo(0, 0, 1, RevealIn);
        // A beat behind the sidebar, so the window assembles left to right.
        await Task.Delay(50);
        _ = Motion(TitleBarRow, affectsGlass: false).AnimateTo(0, 0, 1, RevealIn);
        _ = Motion(ChatColumn).AnimateTo(0, 0, 1, RevealIn);
    }

    private async Task FadeOutStartupCardAsync()
    {
        await Motion(StartupCard).AnimateTo(0, -8, 0, SplashOut);
        _startupBreathing?.Stop();
        _startupBreathing = null;
        StartupOverlay.Visibility = Visibility.Collapsed; // and its glass leaves the scene
    }

    // ---- switching spaces -----------------------------------------------------------------------

    /// <summary>Bumped per switch: a switch overtaken by a newer one mid-flight bows out and
    /// leaves the swap and the entrance to the newer one.</summary>
    private int _spaceTransition;
    /// <summary>Where the switch in flight is going, before the view model has moved there.</summary>
    private ChatSpace? _pendingSpace;

    /// <summary>
    /// The switch as a spatial move: the pages slide out toward the space being left, the
    /// transcript and session list swap while nothing is visible, and the new pages slide in from
    /// the side the new space sits on. Only the lists' item panels move -- their panels stay put
    /// and clip them, which is also what keeps the bubbles' glass inside the chat slab.
    /// </summary>
    private async Task SwitchSpaceAnimatedAsync(ChatSpace target)
    {
        if (target == (_pendingSpace ?? ChatViewModel.Space)) return;
        _pendingSpace = target;
        var id = ++_spaceTransition;
        SetWorkspaceDropdownOpen(false);
        SaveSpace(target);

        // +1: moving right (to Workspace), so content leaves to the left and arrives from the right.
        var direction = target == ChatSpace.Workspace ? 1 : -1;
        var pages = SpacePages();
        await Task.WhenAll(pages.Select(p => Motion(p).AnimateTo(-direction * PageShift, 0, 0, PageOut)));
        if (id != _spaceTransition) return;

        SessionsViewModel.Space = target;
        SessionsViewModel.Sessions.Clear(); // don't show the other space's list while it loads
        var load = Task.WhenAll(ChatViewModel.SwitchSpaceAsync(target), SessionsViewModel.RefreshAsync());
        // Capped like the reveal: a slow fetch fills in on screen rather than hold the page blank.
        await Task.WhenAny(load, Task.Delay(600));
        if (id != _spaceTransition) return;
        _pendingSpace = null;

        // Re-read: a list can rebuild its panel while its items are replaced.
        foreach (var page in SpacePages())
        {
            var motion = Motion(page);
            motion.Set(direction * PageShift, 0, 0);
            _ = motion.AnimateTo(0, 0, 1, PageIn);
        }
    }

    /// <summary>What moves on a space switch: the two lists' item panels, once realised.</summary>
    private List<UIElement> SpacePages()
    {
        var pages = new List<UIElement>(2);
        if (MessageList.ItemsPanelRoot is { } messages) pages.Add(messages);
        if (SessionsList.ItemsPanelRoot is { } sessions) pages.Add(sessions);
        return pages;
    }

    // ---- sheets -----------------------------------------------------------------------------

    /// <summary>What the window's own content fades to while a sheet is open.</summary>
    private const double SheetBackdropOpacity = 0.08;

    /// <summary>
    /// Presents a sheet (Settings, New workspace) in <see cref="SettingsHost"/>: the host fades up
    /// -- scrim, sheet glass and controls together -- while the sheet itself rises into place,
    /// and the window's text fades back (see <see cref="SetSheetOpen"/>).
    /// </summary>
    private void OpenSheet(UserControl pane, UIElement surface)
    {
        SettingsHost.Children.Add(pane);
        SettingsHost.Visibility = Visibility.Visible;
        Motion(SettingsHost).Set(0, 0, 0);
        Motion(surface).Set(0, SheetRise, 1);
        _ = Motion(SettingsHost).AnimateTo(0, 0, 1, SheetIn);
        _ = Motion(surface).AnimateTo(0, 0, 1, SheetIn);
        SetSheetOpen(true);
    }

    private bool _sheetClosing;

    private async void CloseSheet(UIElement surface)
    {
        if (_sheetClosing) return;
        _sheetClosing = true;
        try
        {
            SetSheetOpen(false);
            _ = Motion(surface).AnimateTo(0, SheetRise * 0.5, 1, SheetOut);
            await Motion(SettingsHost).AnimateTo(0, 0, 0, SheetOut);
            SettingsHost.Children.Clear();
            SettingsHost.Visibility = Visibility.Collapsed;
            _motions.Remove(surface); // the pane is gone; a new one gets its own
        }
        finally
        {
            _sheetClosing = false;
        }
    }

    /// <summary>
    /// A sheet in <see cref="SettingsHost"/> is a glass slab, and glass is drawn in the swapchain
    /// *behind* the whole XAML tree -- so the sheet's frost and tint fall on the wallpaper and on
    /// the panels' glass, and never on the window's own text, which paints on top of them. No
    /// amount of tint can stop the chat and sidebar labels reading through a sheet; only taking
    /// them out of the XAML layer can, which is what this does. XAML opacity only (the
    /// transitions don't touch glass): every panel's glass stays exactly where it was, for the
    /// sheet to frost, and only the text that was competing with the sheet's goes.
    /// </summary>
    private void SetSheetOpen(bool open)
    {
        var to = open ? SheetBackdropOpacity : 1.0;
        _ = Motion(TitleBarRow, affectsGlass: false).AnimateTo(0, 0, to, Backdrop);
        _ = Motion(BodyGrid, affectsGlass: false).AnimateTo(0, 0, to, Backdrop);
    }
}
