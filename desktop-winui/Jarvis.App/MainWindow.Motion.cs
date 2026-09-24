using Jarvis_App.ViewModels;
using Jarvis_Glass;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media.Animation;

namespace Jarvis_App;

/// <summary>
/// The window's motion: the startup sequence, the spatial switch between the two spaces, and
/// sheets rising in. Everything moves through <see cref="GlassTransition"/> -- springs, not
/// eased curves, and only transforms, opacity and the glass's material, so nothing re-lays out
/// mid-flight -- which moves, fades and materialises each element's glass with its XAML.
///
/// Budget: every glass shape that moves re-renders its layer and every layer above it each
/// frame, and frost is the expensive pass. So the space switch moves only the two lists' item
/// panels (message bubbles and session rows, which sit inside their panels' clip), never the
/// panels; the startup hand-off is the one time the panels themselves move and condense.
/// </summary>
public sealed partial class MainWindow
{
    // Springs, as (response s, damping ratio). Entrances land with a whisper of overshoot
    // (0.86, SwiftUI's .snappy territory); exits are critically damped and quick. Condensing
    // (clear glass -> frosted) is slow and critically damped: frost that bounced would flicker.
    private static readonly GlassTransition.Spring CardArrive = new(0.55, 0.9);
    private static readonly GlassTransition.Spring Condense = new(0.8, 1.0);
    private static readonly GlassTransition.Spring CardDissolve = new(0.5, 1.0);
    private static readonly GlassTransition.Spring Evaporate = new(0.3, 1.0);
    private static readonly GlassTransition.Spring PanelIn = new(0.55, 0.86);
    private static readonly GlassTransition.Spring ProgressFill = new(0.5, 1.0);
    private static readonly GlassTransition.Spring PageOut = new(0.22, 1.0);
    private static readonly GlassTransition.Spring PageIn = new(0.42, 0.86);
    private static readonly GlassTransition.Spring SheetIn = new(0.45, 0.86);
    private static readonly GlassTransition.Spring SheetOut = new(0.26, 1.0);
    private static readonly GlassTransition.Spring Backdrop = new(0.3, 1.0);

    /// <summary>DIPs a panel rises through as it materialises.</summary>
    private const double RevealRise = 14;
    /// <summary>Scale a panel materialises from, and the card swells to as it dissolves.</summary>
    private const double PanelFromScale = 0.97;
    private const double CardSwell = 1.12;
    /// <summary>Delay between panels, so the window assembles in reading order.</summary>
    private static readonly TimeSpan PanelStagger = TimeSpan.FromMilliseconds(60);
    /// <summary>How long a panel is clear glass before its frost and tint come in.</summary>
    private static readonly TimeSpan CondenseLag = TimeSpan.FromMilliseconds(120);
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
    //
    // Materialise + staged assemble. The card arrives as clear glass (a lens over the wallpaper,
    // no frost or tint) and condenses; a line fills on real steps while the backend boots; then
    // the card swells and evaporates while the panels materialise in reading order -- each one
    // clear glass rising into place, frosting a beat later.

    private readonly long _launchedAt = System.Diagnostics.Stopwatch.GetTimestamp();
    private Storyboard? _startupBreathing;
    private bool _revealed;
    /// <summary>The session-list fetch the first ActiveProjectChanged started; the reveal waits
    /// for it so the sidebar doesn't fill in after the panels have landed.</summary>
    private Task? _sessionsRefresh;

    /// <summary>The panels in the order they materialise.</summary>
    private UIElement[] StartupPanels => new UIElement[] { SidebarSlab, HeaderSlab, MessagesSlab, ComposerSlab };

    /// <summary>Called from the constructor, before anything has published glass: the panels
    /// start invisible, clear and lowered, and the startup card arrives in their place.</summary>
    private void BeginStartup()
    {
        StartupCard.Layer = GlassLayers.Sheet; // over the panels while they arrive beneath it
        BodyGrid.IsHitTestVisible = false;      // invisible is not untouchable in XAML
        foreach (var panel in StartupPanels)
        {
            Motion(panel).Set(0, RevealRise, 0, PanelFromScale, material: 0);
        }
        Motion(TitleBarRow, affectsGlass: false).Set(0, 0, 0);

        var card = Motion(StartupCard);
        card.Set(0, 0, 0, 0.94, material: 0);
        _ = card.AnimateTo(0, 0, 1, CardArrive);
        _ = CondenseAfterAsync(card, TimeSpan.FromMilliseconds(180));
        SetStartupStep(0.1, "Starting the backend");

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

    private static async Task CondenseAfterAsync(GlassTransition motion, TimeSpan lag)
    {
        await Task.Delay(lag);
        await motion.AnimateMaterial(1, Condense);
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
        if (!_revealed) SetStartupStep(0.45, LoadSavedSpace() == ChatSpace.Freeform ? "Opening Freeform" : "Loading workspaces");
        await ProjectsViewModel.RefreshAsync();
        if (_revealed) return;
        SetStartupStep(0.75, "Loading sessions");
        // RefreshAsync returns once the UI update is queued; that update (ActiveProjectChanged)
        // starts the sessions fetch. Capped: a slow list fills in on screen rather than hold the
        // window shut.
        await Task.Yield();
        await Task.WhenAny(_sessionsRefresh ?? Task.CompletedTask, Task.Delay(1500));
        SetStartupStep(1.0, "Ready");
        Reveal("state loaded");
    }

    // ---- the progress line --------------------------------------------------------------------

    private double _progress, _progressVelocity, _progressTarget;
    private bool _progressRunning;
    private double _progressLast = double.NaN;

    /// <summary>Moves the line to <paramref name="fraction"/> on a spring (XAML only: a
    /// ScaleTransform on a fixed-width fill, so the card never re-lays out).</summary>
    private void SetStartupStep(double fraction, string caption)
    {
        StartupStatus.Text = caption;
        _progressTarget = Math.Max(_progressTarget, fraction);
        if (_progressRunning) return;
        _progressRunning = true;
        _progressLast = double.NaN;
        GlassMotion.Frame(StartupProgress, now =>
        {
            var dt = double.IsNaN(_progressLast) ? 0 : Math.Clamp(now - _progressLast, 0.001, 0.05);
            _progressLast = now;
            const double MaxStep = 1.0 / 240.0;
            var steps = Math.Max(1, (int)Math.Ceiling(dt / MaxStep));
            var h = dt / steps;
            for (var i = 0; i < steps && dt > 0; i++)
            {
                var accel = (_progressTarget - _progress) * ProgressFill.Stiffness - _progressVelocity * ProgressFill.Damping;
                _progressVelocity += accel * h;
                _progress += _progressVelocity * h;
            }
            var settled = Math.Abs(_progressTarget - _progress) < 0.002 && Math.Abs(_progressVelocity) < 0.02;
            if (settled) _progress = _progressTarget;
            StartupProgressScale.ScaleX = Math.Clamp(_progress, 0, 1);
            if (settled) _progressRunning = false;
            return !settled;
        });
    }

    // ---- the hand-off ---------------------------------------------------------------------------

    private async void Reveal(string why)
    {
        if (_revealed) return;
        _revealed = true;
        App.Log($"startup: revealing ({why}) after {System.Diagnostics.Stopwatch.GetElapsedTime(_launchedAt).TotalMilliseconds:F0} ms");

        // Let the card finish condensing and the line finish filling if the backend beat them
        // (an already-running backend answers well inside the card's entrance), so the sequence
        // never reads as a flicker.
        var shown = System.Diagnostics.Stopwatch.GetElapsedTime(_launchedAt);
        var minimum = TimeSpan.FromMilliseconds(1100);
        if (shown < minimum) await Task.Delay(minimum - shown);
        if (_progressRunning) await Task.Delay(250);

        BodyGrid.IsHitTestVisible = true;
        _ = DissolveStartupCardAsync();
        _ = Motion(TitleBarRow, affectsGlass: false).AnimateTo(0, 0, 1, PanelIn);
        foreach (var panel in StartupPanels)
        {
            _ = MaterialisePanelAsync(panel);
            await Task.Delay(PanelStagger);
        }
    }

    /// <summary>Clear glass rising into place, then its frost and tint condensing onto it.</summary>
    private async Task MaterialisePanelAsync(UIElement panel)
    {
        var motion = Motion(panel);
        _ = motion.AnimateTo(0, 0, 1, PanelIn);
        await Task.Delay(CondenseLag);
        await motion.AnimateMaterial(1, Condense);
    }

    /// <summary>The card swells and evaporates: its frost goes first, then the lens itself.</summary>
    private async Task DissolveStartupCardAsync()
    {
        var card = Motion(StartupCard);
        _ = card.AnimateMaterial(0, Evaporate);
        await card.AnimateTo(0, 0, 0, CardDissolve, CardSwell);
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
