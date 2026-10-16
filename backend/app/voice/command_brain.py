"""Voice-mode command brain: Laya *selects*, code generates candidates and executes.

Modelled on jev-voice's ``brain.py`` (https://github.com/kevinbadi/jev-voice), with two changes
for a local Laya instead of the hosted Jev:

- **Two stages, not one speculative fan-out.** Laya's cost grows with the number of questions
  and there is no network round trip to hide, so stage 1 asks only what kind of action this is
  (plus "compound?"), and stage 2 asks just the one to three questions that action needs.
- **No question has more than ~20 options.** Laya degrades past that (its head budget is
  shared by every option), so the installed-app list is pre-filtered in code to a short list.

Nothing here generates text. Every free-text value (what to type, the search query, a reminder's
wording) is a span cut out of the transcript by regex, and Laya picks which span is the payload.
Text that is *not* in the utterance ("reply saying I'll be late, politely") is the ``compose``
action and goes to the small LLM. This module is pure Python: the caller supplies ``predict``
(a Laya ``Agent.predict``, or an HTTP client to a Laya sidecar).
"""
from __future__ import annotations

import difflib
import functools
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

Predict = Callable[[dict, dict], dict]

# Short descriptions on purpose: instruction + every option share Laya's head budget.
ACTIONS: dict[str, str] = {
    "open_app": "open, launch or switch to an application (Chrome, Spotify, Notepad, VS Code)",
    "open_website": "go to a website by name or address, no search (youtube, gmail, github.com)",
    "web_search": "search the web or a named site in the browser (google X, search youtube for X)",
    "type_text": "type or dictate the exact words they say into the focused window",
    "compose": "write new text for them that they did not dictate word for word (a reply, an email) and type it",
    "shortcut": "press a key or keyboard shortcut (enter, copy, paste, undo, new tab, close tab, save)",
    "scroll": "scroll up or down, or to the top or bottom",
    "volume": "change the sound volume, mute or unmute",
    "media": "play, pause, skip or go back a song or video",
    "open_folder": "open a folder (downloads, documents, desktop, pictures)",
    "system": "lock the PC, show the desktop, take a screenshot, turn the screen off",
    "reminder": "set a reminder or a timer",
    "question": "asks for information: facts, weather, news, what is on screen or playing, system status",
    "stop": "turn voice mode off or stop listening",
    "none": "small talk, thanks, or speech not meant as a command",
}

SHORTCUTS: dict[str, str] = {
    "enter": "press enter / submit",
    "escape": "escape / cancel",
    "tab": "tab key",
    "backspace": "backspace",
    "delete_word": "delete the previous word",
    "copy": "copy",
    "paste": "paste",
    "cut": "cut",
    "undo": "undo",
    "redo": "redo",
    "select_all": "select all",
    "save": "save",
    "find": "find in page",
    "new_tab": "new tab",
    "close_tab": "close tab",
    "reopen_tab": "reopen closed tab",
    "next_tab": "next tab",
    "previous_tab": "previous tab",
    "back": "go back a page",
    "forward": "go forward a page",
    "reload": "reload / refresh",
    "address_bar": "focus the address bar",
    "close_window": "close the window / quit the app",
    "minimize": "minimize the window",
    "maximize": "maximize / full screen",
    "switch_app": "switch to the previous app (alt-tab)",
    "zoom_in": "zoom in / bigger",
    "zoom_out": "zoom out / smaller",
}

# Shortcuts that throw work away: the permission gate asks before these (PLAN 4.9).
DESTRUCTIVE_SHORTCUTS = frozenset({"close_tab", "close_window", "cut", "delete_word", "backspace"})

SITES: dict[str, str] = {
    "youtube": "https://www.youtube.com",
    "gmail": "https://mail.google.com",
    "google": "https://www.google.com",
    "github": "https://github.com",
    "reddit": "https://www.reddit.com",
    "wikipedia": "https://www.wikipedia.org",
    "amazon": "https://www.amazon.com",
    "netflix": "https://www.netflix.com",
    "twitter": "https://x.com",
    "instagram": "https://www.instagram.com",
    "whatsapp": "https://web.whatsapp.com",
    "chatgpt": "https://chatgpt.com",
    "claude": "https://claude.ai",
    "huggingface": "https://huggingface.co",
    "google drive": "https://drive.google.com",
    "google maps": "https://maps.google.com",
}

SEARCH_ENGINES: dict[str, str] = {
    "google": "https://www.google.com/search?q={q}",
    "youtube": "https://www.youtube.com/results?search_query={q}",
    "amazon": "https://www.amazon.com/s?k={q}",
    "wikipedia": "https://en.wikipedia.org/w/index.php?search={q}",
    "github": "https://github.com/search?q={q}",
    "reddit": "https://www.reddit.com/search/?q={q}",
    "google maps": "https://www.google.com/maps/search/{q}",
    "google images": "https://www.google.com/search?tbm=isch&q={q}",
    "huggingface": "https://huggingface.co/search/full-text?q={q}",
}

FOLDERS = ("downloads", "documents", "desktop", "pictures", "music", "videos", "home", "this pc")

VOLUME_OPS: dict[str, str] = {
    "up": "louder / turn it up",
    "down": "quieter / turn it down",
    "up_a_lot": "much louder",
    "down_a_lot": "much quieter",
    "mute": "mute / silence",
    "unmute": "unmute / sound back on",
    "max": "maximum volume",
    "set_level": "set it to a specific number",
}

MEDIA_OPS: dict[str, str] = {
    "play": "play / resume",
    "pause": "pause / stop for now",
    "next": "next track / skip",
    "previous": "previous track / go back a song",
}

SYSTEM_OPS: dict[str, str] = {
    "lock": "lock the PC",
    "show_desktop": "show the desktop / minimize everything",
    "screenshot": "take a screenshot",
    "screen_off": "turn the screen off",
}

APP_SHORTLIST = 10  # Laya degrades past ~20 options; a short list also keeps the head small.

# --------------------------------------------------------------------------- candidates

_TEXT_PATTERNS = [
    r"^(?:please\s+)?(?:can you\s+|could you\s+)?(?:type|write|enter|dictate|input|put|insert|paste)"
    r"(?:\s+in|\s+out|\s+down|\s+the\s+words?|\s+the\s+text|\s+this|\s+that)?[:,]?\s+(?P<t>.+)$",
    r"^(?:please\s+)?(?:can you\s+|could you\s+)?(?:search|google|look\s*up|find(?:\s+me)?|look\s+for|show\s+me)"
    r"(?:\s+(?:on|in)\s+\w+(?:\s+\w+)?)?(?:\s+for)?[:,]?\s+(?P<t>.+)$",
    r"^(?:please\s+)?(?:search|look\s*up|find)\s+(?:on\s+)?[a-z]+(?:\s+(?:maps|images|drive|face))?\s+for\s+(?P<t>.+)$",
    r"^(?:[a-z]+\s+)?[a-z]+\s+search\s+(?:for\s+)?(?P<t>.+)$",  # "youtube search X", "google search X"
    r"^.*?\b(?:for|about|of)\s+(?P<t>.+)$",
    r"[\"“'](?P<t>[^\"”']+)[\"”']",
]
_TITLE = re.compile(r"\b(?:called|titled|named|that says|saying)\s+(?P<t>.+)$", re.I)
# "... on amazon", "... on google images", "... online": where to do it, not part of the payload.
# Kept as an *extra* candidate, never a replacement ("meet me in the park" must survive).
_TRAILING_IN_APP = re.compile(
    r"(?:\s+(?:in|into|inside|on|using)\s+(?:the\s+)?[a-z][\w.]*(?:\s+[a-z][\w.]*)?(?:\s+app)?|\s+online)\s*[.!?]?$",
    re.I,
)
_TRAILING_SUBMIT = re.compile(
    r"[\s,.]*(?:and|then)?\s*(?:hit|press|and)\s+(?:enter|return|send|submit)\s*[.!]?$", re.I
)
_POLITE_TAIL = re.compile(r"(?:[\s,]+(?:please|for me|now|right now|real quick|thanks|thank you))+\s*[.!?]?$", re.I)
_SPLIT_COMPOUND = re.compile(r"\s*(?:,\s*)?\b(?:and then|then|and also|and)\b\s*", re.I)
_SUBMIT_ONLY = re.compile(r"^(?:then\s+)?(?:hit|press|and)?\s*(?:enter|return|send|submit)(?:\s+it)?$", re.I)
_WAKE = re.compile(r"^\s*(?:hey\s+|ok\s+|okay\s+)?jarvis[\s,.!?]*", re.I)


def strip_wake_word(utterance: str) -> str:
    return _WAKE.sub("", utterance or "").strip()


# A search runs only when the user asked for one. On device Laya picked web_search, confident,
# for follow-ups it can't resolve ("can you download it?", "I meant the mod, not the movie",
# "click into the X server") and searched the whole sentence or just "it".
_SEARCH_CUE = re.compile(
    r"\b(?:search|google|look\s+(?:up|for)|find|bing|show\s+me|(?:pictures?|images?|photos?|videos?)\s+of|"
    + "|".join(re.escape(e) for e in SEARCH_ENGINES) + r")\b",
    re.I,
)
_EMPTY_QUERY_WORDS = frozenset(
    "it its that this these those them they one ones the a an for of about on please link links "
    "result results thing stuff something".split()
)


# An unplaced site ("go to the rust docs") is searched for instead -- only when the user asked to
# go somewhere and named it. "Can you click into the rising ecstasy server?" (Laya: open_website,
# 0.89) and "open that thing we talked about" are for MiniCPM.
_NAV_CUE = re.compile(r"\b(?:go\s+to|open|visit|navigate\s+to|take\s+me\s+to|pull\s+up|bring\s+up|load)\b", re.I)
_REFERS_BACK = re.compile(r"\b(?:it|that|this|them|those|these|one)\b", re.I)


def site_fallback_is_grounded(utterance: str) -> bool:
    u = strip_wake_word(utterance or "")
    return bool(_NAV_CUE.search(u) or _SEARCH_CUE.search(u)) and not _REFERS_BACK.search(u)


def search_is_grounded(utterance: str, query: str) -> bool:
    """True when ``utterance`` asks for a search and ``query`` says what for. Otherwise the turn
    belongs to MiniCPM, which sees the conversation and can work out what "it" is."""
    if not _SEARCH_CUE.search(utterance or ""):
        return False
    words = re.findall(r"[a-z0-9.+#']+", (query or "").lower())
    return any(w not in _EMPTY_QUERY_WORDS for w in words)


def _clean(s: str) -> str:
    s = s.strip().strip("\"“”'")
    s = _POLITE_TAIL.sub("", s).strip()
    s = _TRAILING_SUBMIT.sub("", s).strip()
    s = _POLITE_TAIL.sub("", s).strip()
    return s.rstrip(" .!?").strip()


def text_candidates(utterance: str) -> dict[str, str]:
    """Spans that might be the payload text (to type, to search for). Laya picks; code never guesses."""
    cands: list[str] = []

    def add(t: str) -> None:
        t = _clean(t)
        if t and t not in cands:
            cands.append(t)

    m = _TITLE.search(utterance)
    if m:
        add(m.group("t"))
    for pat in _TEXT_PATTERNS:
        m = re.search(pat, utterance, flags=re.I)
        if m:
            add(m.group("t"))
            add(_TRAILING_IN_APP.sub("", _clean(m.group("t"))))
    del cands[7:]
    add(utterance)  # always offered: "the whole thing" is a legitimate payload
    if not cands:
        cands.append(utterance.strip() or "(nothing)")
    return {f"c{i}": c for i, c in enumerate(cands[:8])}


_REMIND_TEXT = re.compile(
    r"\b(?:to|that|about)\s+(?P<t>.+?)(?:\s+(?:in|at|on|tomorrow|tonight|every|after)\b.*)?$", re.I
)
_WHEN_PATTERNS = [
    r"\bfor\s+(?:an?\s+|\d+\s+|a few\s+|half an\s+)(?:\w+\s+)?(?:seconds?|minutes?|mins?|hours?|hrs?)\b",
    r"\bin\s+(?:an?\s+|\d+\s+|a few\s+|half an\s+)?(?:\w+\s+)?(?:seconds?|minutes?|mins?|hours?|hrs?)\b",
    r"\b(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)",
    r"\bat\s+\d{1,2}(?::\d{2})?\b",
    r"\b(?:tomorrow|tonight|this evening|this afternoon)(?:\s+(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?",
]


def reminder_candidates(utterance: str) -> tuple[dict[str, str], dict[str, str]]:
    """(what, when) candidate spans for a reminder or timer."""
    whens: list[str] = []
    for pat in _WHEN_PATTERNS:
        for m in re.finditer(pat, utterance, flags=re.I):
            w = m.group(0).strip()
            if w and w not in whens:
                whens.append(w)
    if not whens:
        whens.append("(no time given)")
    whats: list[str] = []
    m = _REMIND_TEXT.search(utterance)
    if m:
        whats.append(_clean(m.group("t")))
    stripped = utterance
    for w in whens:
        stripped = stripped.replace(w, " ")
    stripped = re.sub(r"^\s*(?:please\s+)?(?:remind me|set (?:a |an )?(?:reminder|timer|alarm)(?:\s+for)?)\s*", "",
                      stripped, flags=re.I)
    for t in (_clean(stripped), "(no text given)"):
        if t and t not in whats:
            whats.append(t)
    return ({f"w{i}": w for i, w in enumerate(whats[:5])},
            {f"t{i}": t for i, t in enumerate(whens[:5])})


_NUMBER = re.compile(r"\b(\d{1,3})\s*(?:%|percent)?\b")


def number_candidates(utterance: str) -> list[int]:
    return [n for n in (int(x) for x in _NUMBER.findall(utterance)) if 0 <= n <= 100]


_DOMAIN = re.compile(r"\b([a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|org|net|io|ai|dev|co|uk|in|app|gg|tv))\b", re.I)
_SITE_WORD = re.compile(
    r"\b(?:go to|open|visit|pull up|bring up|load|navigate to|take me to)\s+(?:the\s+)?(?:website\s+|site\s+)?"
    r"([a-z0-9][a-z0-9 .-]*?)(?:\s+(?:website|site|page|homepage))?\s*[.!?]?$",
    re.I,
)


_NOT_A_SITE = frozenset(
    "it that this the docs documentation store shop page site website home homepage news "
    "settings app link links one".split()
)


def domain_guess(utterance: str) -> Optional[str]:
    spoken = re.sub(r"\s+dot\s+", ".", utterance, flags=re.I)
    m = _DOMAIN.search(spoken)
    if m:
        return m.group(1).lower()
    m = _SITE_WORD.search(spoken)
    if m:
        words = m.group(1).strip().lower().split()
        # Only a one-word name becomes <name>.com ("go to reddit"). Gluing two words was wrong
        # more often than right ("rust docs" -> rustdocs.com, "steam store" -> steamstore.com,
        # even "hacker news" -> hackernews.com); those fall back to a search, which finds them.
        if len(words) != 1 or words[0] in _NOT_A_SITE:
            return None
        return words[0] if "." in words[0] else f"{words[0]}.com"
    return None


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


# A misheard name ("dicsord" ~ discord, 0.86) is a near-spelling; a different app is not
# ("photoshop" ~ photos, 0.80: Laya then confidently opened Photos, 2026-10-02).
_NEAR_SPELLING = 0.84


@functools.lru_cache(maxsize=4096)
def _app_words(app: str) -> frozenset[str]:
    return frozenset(_words(app))


_STOP_WORDS = frozenset(
    "open launch start switch to the a an app application please can you could jarvis bring up go "
    "and then run fire".split()
)


def app_shortlist(utterance: str, apps: list[str], n: int = APP_SHORTLIST) -> list[str]:
    """The installed apps most plausibly meant, best first, at most ``n``: shared words, then
    near-spellings of the utterance's words. Laya picks among these (or none)."""
    said = {w for w in _words(utterance) if w not in _STOP_WORDS and len(w) >= 2}
    if not said:
        return []
    matchers = [difflib.SequenceMatcher(None, b=s) for s in said]  # b is cached per word
    scored = []
    for app in apps:
        aw = _app_words(app)
        overlap = len(said & aw)
        fuzzy = 0.0
        for sm in matchers:
            for a in aw:
                sm.set_seq1(a)
                if sm.real_quick_ratio() >= _NEAR_SPELLING and sm.quick_ratio() >= _NEAR_SPELLING:
                    fuzzy = max(fuzzy, sm.ratio())
        if overlap or fuzzy >= _NEAR_SPELLING:
            scored.append((overlap, fuzzy, -len(app), app))
    scored.sort(reverse=True)
    return [a for *_, a in scored[:n]]


_COMMAND_START = re.compile(
    r"^(?:open|launch|start|switch|go|search|google|look|find|type|write|enter|press|hit|scroll|close|"
    r"copy|paste|undo|save|play|pause|skip|mute|unmute|turn|set|remind|show|take|lock|minimize|"
    r"maximize|refresh|reload|select)\b", re.I,
)


def looks_compound(utterance: str) -> bool:
    """Code's backstop for Laya's compound answer: 'X and Y' where Y starts like a command.
    ('type salt and pepper' stays one command -- 'pepper' is no verb.)"""
    parts = split_compound(utterance)
    return len(parts) > 1 and all(_COMMAND_START.match(p) for p in parts[1:])


def split_compound(utterance: str) -> list[str]:
    """'open notepad and type hello' -> two parts. A trailing 'hit enter' stays with the typing."""
    parts = [p.strip(" ,.") for p in _SPLIT_COMPOUND.split(utterance)]
    parts = [p for p in parts if len(p) > 1]
    if len(parts) > 1 and _SUBMIT_ONLY.match(parts[-1]):
        parts = parts[:-1]
        if len(parts) == 1:
            return [utterance]
        parts[-1] = parts[-1] + " and hit enter"
    return parts


# --------------------------------------------------------------------------- questions


def _choice(instructions: str, criteria: dict) -> dict:
    return {"type": "choice", "instructions": instructions, "criteria": criteria}


def _yes_no(instructions: str, yes: str, no: str) -> dict:
    # A two-option choice with neutral keys, not a `noul`: Laya's card reports noul answers
    # following the false/true labels instead of the state on the English checkpoint (#156).
    return _choice(instructions, {"A": yes, "B": no})


def stage1_questions() -> dict:
    return {
        "action": _choice(
            "The user spoke to their Windows PC's voice assistant; `utterance` is the transcript. "
            "Which kind of action do they want right now?",
            ACTIONS,
        ),
        "compound": _yes_no(
            "Does `utterance` ask for two or more separate actions one after another?",
            "yes, two or more separate actions (open notepad and type hello)",
            "no, exactly one action",
        ),
    }


def stage2_questions(action: str, ctx: dict) -> dict:
    """Only the questions ``action`` needs. ``ctx`` holds the candidates built for the utterance."""
    q: dict[str, Any] = {}
    if action == "open_app":
        q["app"] = _choice(
            "Which installed application in `apps` do they mean? Match on meaning: 'chrome' is Google "
            "Chrome, 'code' is Visual Studio Code. Choose none if no listed app matches.",
            {**{a: None for a in ctx["apps"]}, "none": "no listed app matches"},
        )
    elif action == "open_website":
        q["site"] = _choice(
            "Which website do they want to open? Choose other if it is not listed.",
            {**{s: None for s in SITES}, "other": "a site not in this list"},
        )
    elif action == "web_search":
        q["engine"] = _choice(
            "Where should the search run? If they do not name a site, choose google.",
            {e: None for e in SEARCH_ENGINES},
        )
        q["text"] = _choice(
            "`candidates` are spans cut from the utterance. Which one is exactly the search query, "
            "without command words like 'search for' or 'on youtube'?",
            ctx["text"],
        )
    elif action == "type_text":
        q["text"] = _choice(
            "`candidates` are spans cut from the utterance. Which one is exactly the text to type, "
            "without command words like 'type' and without a trailing 'and press enter'?",
            ctx["text"],
        )
        q["submit"] = _yes_no(
            "After typing, do they also want enter pressed ('and hit enter', 'and send it')?",
            "yes, press enter afterwards", "no, only type the text",
        )
    elif action == "compose":
        q["submit"] = _yes_no(
            "After the text is written, do they want it sent or enter pressed?",
            "yes, send it / press enter", "no, just write it",
        )
    elif action == "shortcut":
        q["shortcut"] = _choice("Which key or keyboard shortcut do they want pressed?", SHORTCUTS)
    elif action == "scroll":
        q["scroll_dir"] = _choice(
            "Which way should it scroll?",
            {"down": "down / further", "up": "up / back up", "top": "to the very top", "bottom": "to the very bottom"},
        )
        q["scroll_amount"] = _choice(
            "How far?",
            {"little": "a little / a bit", "page": "about one screen (default)", "a_lot": "a lot / way down"},
        )
    elif action == "volume":
        q["volume_op"] = _choice("What change to the volume?", VOLUME_OPS)
        if ctx["numbers"]:
            q["level"] = _choice(
                "Which number is the volume level they asked for?",
                {str(n): None for n in ctx["numbers"]},
            )
    elif action == "media":
        q["media_op"] = _choice("What should playback do?", MEDIA_OPS)
    elif action == "open_folder":
        q["folder"] = _choice("Which folder?", {f: None for f in FOLDERS})
    elif action == "system":
        q["system_op"] = _choice("Which system action?", SYSTEM_OPS)
    elif action == "reminder":
        q["remind_what"] = _choice(
            "`what` are spans cut from the utterance. Which one is what to be reminded of?", ctx["what"]
        )
        q["remind_when"] = _choice(
            "`when` are spans cut from the utterance. Which one says when the reminder should fire?",
            ctx["when"],
        )
    return q


# --------------------------------------------------------------------------- plan


@dataclass
class Plan:
    utterance: str
    action: str
    confidence: float
    args: dict[str, Any] = field(default_factory=dict)
    compound: bool = False
    answers: dict[str, Any] = field(default_factory=dict)
    latency_ms: dict[str, int] = field(default_factory=dict)

    def __str__(self) -> str:
        a = ", ".join(f"{k}={v!r}" for k, v in self.args.items())
        ms = "+".join(str(v) for v in self.latency_ms.values())
        return f"{self.action}({a}) conf={self.confidence:.2f} compound={self.compound} {ms}ms"


def _conf(ans: dict) -> float:
    return float(ans.get("answer_confidence", ans.get("confidence", 0.0)))


def build_context(utterance: str, apps: list[str]) -> dict:
    what, when = reminder_candidates(utterance)
    return {
        "text": text_candidates(utterance),
        "apps": app_shortlist(utterance, apps),
        "numbers": number_candidates(utterance),
        "what": what,
        "when": when,
    }


def _state(utterance: str, ctx: dict, action: Optional[str], foreground: str) -> dict:
    """What Laya reads. Only the candidate lists the stage-2 questions refer to are included,
    so the state stays short (it shares the 512-token window with each question)."""
    st: dict[str, Any] = {"utterance": utterance}
    if foreground:
        st["foreground_window"] = foreground
    if action == "open_app":
        st["apps"] = ctx["apps"]
    elif action in ("web_search", "type_text"):
        st["candidates"] = ctx["text"]
    elif action == "reminder":
        st["what"], st["when"] = ctx["what"], ctx["when"]
    return st


def evaluate(utterance: str, predict: Predict, apps: list[str], foreground: str = "") -> Plan:
    """Two Laya passes -> a Plan. ``predict(state, questions)`` returns Laya's ``answers`` dict."""
    utterance = strip_wake_word(utterance)
    ctx = build_context(utterance, apps)
    lat: dict[str, int] = {}

    t0 = time.perf_counter()
    a1 = predict(_state(utterance, ctx, None, foreground), stage1_questions())
    lat["stage1"] = int((time.perf_counter() - t0) * 1000)
    action = a1["action"]["choice"]
    conf = _conf(a1["action"])
    compound = a1["compound"]["choice"] == "A"

    q2 = stage2_questions(action, ctx)
    a2: dict[str, Any] = {}
    if q2:
        t0 = time.perf_counter()
        a2 = predict(_state(utterance, ctx, action, foreground), q2)
        lat["stage2"] = int((time.perf_counter() - t0) * 1000)

    def pick(key: str) -> tuple[str, float]:
        a = a2[key]
        return a["choice"], _conf(a)

    args: dict[str, Any] = {}
    used: list[float] = [conf]
    if action == "open_app":
        app, c = pick("app") if "app" in a2 else ("none", 0.0)
        args["app"] = None if app == "none" else app
        used.append(c)
    elif action == "open_website":
        site, c = pick("site")
        if site != "other":
            args["url"], args["site"] = SITES[site], site
            used.append(c)
        else:
            dom = domain_guess(utterance)
            if dom:
                args["url"], args["site"] = f"https://{dom}", dom
            elif not site_fallback_is_grounded(utterance):
                action, args = "question", {"unplaced_site": utterance}
            else:
                action = "web_search"  # nothing to navigate to: search for it instead
                q2 = stage2_questions(action, ctx)
                a2 = predict(_state(utterance, ctx, action, foreground), q2)
    if action == "web_search":
        engine, _ = pick("engine")
        tkey, c = pick("text")
        args["engine"], args["query"] = engine, ctx["text"].get(tkey, utterance)
        used.append(c)
        # (A site Laya couldn't place falls back to a search on purpose: that one is exempt.)
        if a1["action"]["choice"] == "web_search" and not search_is_grounded(utterance, args["query"]):
            action, args = "question", {"ungrounded_search": args["query"]}
    elif action == "type_text":
        tkey, c = pick("text")
        args["text"] = ctx["text"].get(tkey, utterance)
        args["submit"] = pick("submit")[0] == "A"
        used.append(c)
    elif action == "compose":
        args["instruction"] = utterance
        args["submit"] = pick("submit")[0] == "A"
    elif action == "shortcut":
        s, c = pick("shortcut")
        args["shortcut"] = s
        used.append(c)
    elif action == "scroll":
        d, c = pick("scroll_dir")
        args["direction"], args["amount"] = d, pick("scroll_amount")[0]
        used.append(c)
    elif action == "volume":
        op, c = pick("volume_op")
        args["op"] = op
        used.append(c)
        if op == "set_level":
            if "level" in a2:
                lv, c2 = pick("level")
                args["level"] = int(lv)
                used.append(c2)
            else:
                used.append(0.0)  # a level with no number said
    elif action == "media":
        op, c = pick("media_op")
        args["op"] = op
        used.append(c)
    elif action == "open_folder":
        f, c = pick("folder")
        args["folder"] = f
        used.append(c)
    elif action == "system":
        op, c = pick("system_op")
        args["op"] = op
        used.append(c)
    elif action == "reminder":
        wkey, c1 = pick("remind_what")
        tkey, c2 = pick("remind_when")
        args["text"], args["when"] = ctx["what"].get(wkey), ctx["when"].get(tkey)
        used += [c1, c2]

    return Plan(utterance, action, min(used), args, compound, {**a1, **a2}, lat)
