using System.Text.RegularExpressions;

namespace Jarvis.Core.Voice;

public enum ConfirmationIntent
{
    /// <summary>Not an answer to the pending question — treat as an ordinary message.</summary>
    Unclear,
    Yes,
    No,
}

/// <summary>
/// Reads a reply to a pending confirmation ("I need your approval to run: git push. Say yes to
/// proceed, or no to cancel, sir.") as yes, no, or neither. Deliberately narrow: it only needs to
/// catch the common spoken and typed forms of an answer, not parse arbitrary intent. Shared by the
/// typed composer and the voice path, since the backend's spoken prompt invites either.
/// </summary>
public static class ConfirmationIntentParser
{
    private static readonly string[] YesWords =
    {
        "yes", "yeah", "yep", "yup", "confirm", "confirmed", "approve", "approved", "affirmative",
        "do it", "go ahead", "proceed",
    };

    /// <summary>Approval is the direction that runs something, and these open plenty of
    /// sentences that are not approvals ("ok so what does that do?"), so they only count as the
    /// whole answer or its last word.</summary>
    private static readonly string[] YesFillerWords = { "ok", "okay", "sure", "fine" };

    private static readonly string[] NoWords =
    {
        "no", "nope", "nah", "cancel", "cancelled", "canceled", "deny", "denied", "negative",
        "stop", "don't", "do not", "abort",
    };

    private static readonly Regex Punctuation = new(@"[^\p{L}\p{N}' ]+", RegexOptions.Compiled);
    private static readonly Regex Whitespace = new(@"\s+", RegexOptions.Compiled);

    public static ConfirmationIntent Parse(string? text)
    {
        if (string.IsNullOrWhiteSpace(text))
        {
            return ConfirmationIntent.Unclear;
        }

        // "Yes, do it." and "jarvis, no" both need to read as one-word answers, so punctuation
        // becomes spacing and the match is on leading or trailing words rather than the whole.
        var normalized = Whitespace.Replace(Punctuation.Replace(text.ToLowerInvariant(), " "), " ").Trim();
        if (normalized.Length == 0)
        {
            return ConfirmationIntent.Unclear;
        }

        var yes = Matches(normalized, YesWords) || Matches(normalized, YesFillerWords, leading: false);
        var no = Matches(normalized, NoWords);

        if (yes && no)
        {
            // "don't do it" / "no, go ahead": a negation up front wins, since cancelling is the
            // direction that runs nothing. "yes... actually no" is not an answer we may act on;
            // the card stays and the user can be asked again.
            return StartsWithAny(normalized, NoWords) ? ConfirmationIntent.No : ConfirmationIntent.Unclear;
        }
        if (yes) return ConfirmationIntent.Yes;
        if (no) return ConfirmationIntent.No;
        return ConfirmationIntent.Unclear;
    }

    private static bool StartsWithAny(string normalized, string[] words)
    {
        foreach (var word in words)
        {
            if (normalized == word || normalized.StartsWith(word + " ", StringComparison.Ordinal))
            {
                return true;
            }
        }
        return false;
    }

    private static bool Matches(string normalized, string[] words, bool leading = true)
    {
        foreach (var word in words)
        {
            if (normalized == word
                || (leading && normalized.StartsWith(word + " ", StringComparison.Ordinal))
                || normalized.EndsWith(" " + word, StringComparison.Ordinal))
            {
                return true;
            }
        }
        return false;
    }
}
