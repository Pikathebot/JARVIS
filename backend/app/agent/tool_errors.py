"""
What kind of failure a tool result is, and the one next step that goes with it (PLAN 4.15 #4,
after harness2's INV rule: tell bad input apart from a timeout, a missing thing, a refusal and a
tool that can't run here, and give one bounded fallback with a stop condition -- never "retry").

Tools report failures as "Error..." text. The kind is read from that text; the next step is a
fixed line appended to the result the model reads (never stored differently, never in the cached
prefix). A second failure of the same tool and kind in one turn gets the stop line instead.
"""
import re
from typing import Optional

# First match wins; the order puts specific wording ahead of generic.
_KINDS: tuple[tuple[str, re.Pattern], ...] = (
    ("timeout", re.compile(r"timed out|time-?out", re.I)),
    ("unavailable", re.compile(
        r"not available (here|in this)|Freeform conversation|not a git repository|only works on|"
        r"package is missing|is not registered|sign-in page|can't be (closed|deleted) from chat", re.I)),
    ("not_permitted", re.compile(r"access denied|outside the (active project )?workspace|refused|permission", re.I)),
    ("bad_input", re.compile(
        r"validation error|invalid argument|cannot be empty|must (be|not)|needs some text|"
        r"no \w+( \w+)? provided|is a (directory|file|web url)|unknown \w+ action|give a level|"
        r"before start_line|not found in '.*'\. The closest part|search block was not found|"
        r"occurrences of the search block|already exists and overwrite", re.I)),
    ("not_found", re.compile(
        r"not found|does not exist|no (running|visible|single)|no such|\b404\b|cannot find", re.I)),
)

# Web lookups share one count per turn: a question searched again and again (the eval's 3-5
# research calls in a turn) never passed on 2026-10-18, but one 2026-10-16 pass took six, so the
# third call gets a hint rather than a refusal.
WEB_FAMILY = frozenset({"web_search", "research"})
WEB_HINT_AT = 3


def web_hint(count: int) -> str:
    return (f"That is {count} web lookups this turn. If this still doesn't answer it, stop searching: "
            "answer from what you have and say what you couldn't confirm.")

NEXT_STEP = {
    "timeout": "",  # the pipeline's own message already says not to repeat the call
    "unavailable": "Next: don't call it again this turn; tell the user it can't run here and why.",
    "not_permitted": "Next: don't retry it; tell the user what was refused.",
    "bad_input": "Next: fix the argument the error names and call it once more; never repeat the same call.",
    "not_found": "Next: check the name or path once (find_files or list_directory for files); "
                 "if it still isn't there, tell the user.",
    "other": "Next: try one different approach at most; if that fails too, tell the user what failed.",
}


def error_kind(result: str) -> Optional[str]:
    """The failure kind of a tool result, or None when it isn't a failure."""
    text = (result or "").lstrip()
    if not text.startswith("Error"):
        return None
    head = text[:400]
    for kind, pattern in _KINDS:
        if pattern.search(head):
            return kind
    return "other"


def stop_line(tool: str) -> str:
    return (f"'{tool}' has now failed the same way twice this turn. Stop calling it; tell the user "
            "what failed and what you tried.")
