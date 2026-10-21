namespace Jarvis_App;

/// <summary>
/// Voice commands and the composer. Three times on 2026-10-20 (and again on retest) a typed
/// "send a draft email to ...@gmail about JARVIS app" went out as just "app": the user said the
/// sentence aloud while typing it, the wake-word path heard "Jarvis", took "app" as the command
/// (backend.log: "Wake word 'Jarvis' detected. Extracted query: 'app'"), and the voice handler
/// cleared the composer before sending it -- wiping the half-typed message. So a wake-word
/// command that arrives while someone is typing is ignored (it is them reading their own
/// message), and a voice command never touches the composer's text.
/// </summary>
public sealed partial class MainWindow
{
    /// <summary>How long after a keystroke in a non-empty composer the user counts as typing.</summary>
    private static readonly TimeSpan TypingWindow = TimeSpan.FromSeconds(10);

    private DateTime _composerKeyAt = DateTime.MinValue;

    private void SetUpComposerWatch()
    {
        ComposerBox.InputKeyDown += (_, _) => { _composerKeyAt = DateTime.UtcNow; WarmModelSoon(); };
    }

    private bool IsTypingInComposer() =>
        !string.IsNullOrWhiteSpace(ComposerBox.Text) && DateTime.UtcNow - _composerKeyAt < TypingWindow;
}
