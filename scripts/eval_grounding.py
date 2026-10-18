"""Specifics in a web answer that appear in nothing the turn read (PLAN 4.15 #7, after harness2's
selector rule "reject invented specifics"). Stdlib only; an eval diagnostic, not a chat-loop check.

Only multi-word capitalised names and numbers with two or more digits count, matched case- and
spacing-insensitively against the user's message, the turn context and every tool result.

Measured 2026-10-18 on Defiant's eval traces: it flagged 9 of 13 failing web answers (invented
ratings, a wrong Nobel year, made-up PEP numbers) and none of 17 passing ones. Wired into the
stream loop as a one-time "these aren't in what you read" nudge it changed no pass (0/15 on the
tasks where it fired, with or without it), took 1.6-3x as long, and the retry either dropped the
parts that were right ("You're right, my apologies, I couldn't confirm...") or invented new
specifics -- so it was taken out of the loop. The eval records the flags per run (``unsourced``)
so fabrication can be counted alongside the pass rate.
"""

from __future__ import annotations

import re

WEB_TOOLS = frozenset({"web_search", "research", "fetch_url"})

_NAME = re.compile(r"\b(?:[A-Z][\w'’-]+(?:\s+(?:of|the|de|and|&)?\s*[A-Z][\w'’-]+)+)\b")
_NUMBER = re.compile(r"\b\d[\d.,:]*\d\b|\bv\d+(?:\.\d+)+\b")
# Sentence openers that join a following name ("See OpenAPI", "It's Wukong Together").
_STOP = {"The", "This", "That", "I", "It", "In", "On", "As", "If", "For", "Here", "There", "Yes", "No",
         "See", "Use", "Try", "Check", "Note", "Also", "Then", "So", "But", "And", "Its", "They", "We", "You",
         "He", "She", "My", "Your", "Our", "A", "An", "At", "By", "From", "With", "To", "Or"}
_GENERIC = {"Current", "Latest", "New", "Top", "Best", "Recent", "Today", "Today's", "Main", "Key", "Popular"}


def specifics(text: str) -> set[str]:
    out: set[str] = set()
    for match in _NAME.findall(text or ""):
        for part in re.split(r"\s+(?:and|&)\s+", match):
            words = [w for w in (re.sub(r"['’]s$", "", w) for w in part.split()) if w not in _STOP]
            while words and words[0] in _GENERIC:
                words = words[1:]
            if len(words) >= 2:
                out.add(" ".join(words))
    out |= set(_NUMBER.findall(text or ""))
    return out


def _norm(text: str) -> str:
    return re.sub(r"[\s’'*_`]+", " ", (text or "").lower())


def unsourced(answer: str, sources: list[str]) -> list[str]:
    """The answer's specifics that none of ``sources`` contains, in the order they appear."""
    haystack = _norm(" ".join(sources))
    found = [s for s in specifics(answer) if _norm(s) not in haystack]
    return sorted(found, key=lambda s: (answer.find(s), s))


def turn_sources(messages: list[dict]) -> list[str]:
    """What the model was given to answer from: user turns (with their turn context) and tool results."""
    out = []
    for m in messages:
        if m.get("role") not in ("user", "tool"):
            continue
        content = m.get("content")
        if isinstance(content, list):
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        if isinstance(content, str):
            out.append(content)
    return out
