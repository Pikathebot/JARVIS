"""The user's calendar (PLAN 4.16 phase B): a local copy that works offline, synced two-way with
the primary Google Calendar when signed in (``app/google/calendar_sync.py``; Google is the master).

Events live in ``calendar.json`` beside the Google token (repo-root ``data/``), in local wall-clock
time: ``"2026-10-20T15:00"``, or a date for an all-day event (whose end is exclusive, as at Google).
A change made here is ``dirty`` (a delete leaves a ``deleted`` tombstone) until a sync has sent it,
so an event added offline goes up when Google is reachable again.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from app.google import auth

logger = logging.getLogger("jarvis.agent.tools.calendar")

LOCK = threading.RLock()  # the Google sync edits calendar.json under the same lock
_OFF_RECORD = "Not saved: this is an off-the-record conversation, and nothing from it is kept."
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_LIST_MAX = 25
_LOOKAHEAD_DAYS = 366 * 10  # how far "the next event" is looked for past an empty span


def store_path() -> Path:
    return auth.DATA_DIR / "calendar.json"


def load() -> dict[str, Any]:
    try:
        state = json.loads(store_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    state.setdefault("events", [])
    return state


def save(state: dict[str, Any]) -> None:
    p = store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    tmp.replace(p)


def new_id(events: list[dict[str, Any]]) -> str:
    taken = {e.get("id") for e in events}
    while True:
        eid = "e" + uuid.uuid4().hex[:4]
        if eid not in taken:
            return eid


def _now() -> datetime:
    return datetime.now()


def _is_ephemeral(session_id: Optional[str]) -> bool:
    from app.agent.tools.memory import _is_ephemeral as check
    return check(session_id)


# --- times ------------------------------------------------------------------------------------

def is_all_day(ev: dict[str, Any]) -> bool:
    return "T" not in ev["start"]


def start_dt(ev: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(ev["start"])


def end_dt(ev: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(ev["end"])


def _stamp(at: datetime, all_day: bool) -> str:
    return f"{at:%Y-%m-%d}" if all_day else f"{at:%Y-%m-%dT%H:%M}"


def label(ev: dict[str, Any]) -> str:
    s, e = start_dt(ev), end_dt(ev)
    if is_all_day(ev):
        days = max(1, (e - s).days)
        span = f"{s:%a %d %b}, all day" + (f" ({days} days)" if days > 1 else "")
    elif e.date() != s.date():
        span = f"{s:%a %d %b %H:%M} - {e:%a %d %b %H:%M}"
    else:
        span = f"{s:%a %d %b %H:%M}-{e:%H:%M}"
    where = f" @ {ev['location']}" if ev.get("location") else ""
    again = " (every year)" if ev.get("repeat") else ""
    return f"{ev['id']}: {span} {ev['title']}{where}{again}"


def _occurrences(ev: dict[str, Any], lo: datetime, hi: datetime) -> list[dict[str, Any]]:
    """The event itself, or for a yearly one, each year's copy that overlaps [lo, hi). Copies keep
    the series' id, so an id from the agenda still names the series."""
    if ev.get("repeat") != "yearly":
        return [ev]
    s, length = start_dt(ev), end_dt(ev) - start_dt(ev)
    out = []
    for year in range(lo.year, hi.year + 1):
        try:
            at = s.replace(year=year)
        except ValueError:  # 29 Feb in a year without one
            at = s.replace(year=year, day=28)
        if at < hi and at + length > lo:
            out.append({**ev, "start": _stamp(at, is_all_day(ev)), "end": _stamp(at + length, is_all_day(ev))})
    return out


def _in_range(events: list[dict[str, Any]], lo: datetime, hi: datetime) -> list[dict[str, Any]]:
    return sorted((o for e in _live(events) for o in _occurrences(e, lo, hi) if start_dt(o) < hi and end_dt(o) > lo),
                  key=start_dt)


_DAY_RE = re.compile(r"\b(today|tonight|tomorrow|(?:next\s+)?(?:" + "|".join(_WEEKDAYS) + r")|\d{4}-\d{2}-\d{2})\b")


def _day(word: str, now: datetime) -> date:
    if word in ("today", "tonight"):
        return now.date()
    if word == "tomorrow":
        return now.date() + timedelta(days=1)
    wd = word.split()[-1]
    if wd in _WEEKDAYS:
        return now.date() + timedelta(days=(_WEEKDAYS.index(wd) - now.weekday()) % 7 or 7)
    return date.fromisoformat(word)


_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_MONTH_DAY_RE = re.compile(r"\b(" + "|".join(_MONTHS) + r")[a-z]*\.?\s+(\d{1,2})\b")
_DAY_MONTH_RE = re.compile(r"\b(\d{1,2})\s+(" + "|".join(_MONTHS) + r")[a-z]*\.?\b")


def _spell_date(text: str, now: datetime) -> str:
    """'sept 19' or '19 sep' -> '2026-09-19' in this year (a year-less date is this year's); an
    impossible one is left as it was, so the caller's error still names it."""
    def iso(month: str, day: str, original: str) -> str:
        try:
            return f"{date(now.year, _MONTHS.index(month) + 1, int(day)):%Y-%m-%d}"
        except ValueError:
            return original

    text = _MONTH_DAY_RE.sub(lambda m: iso(m.group(1), m.group(2), m.group(0)), text)
    return _DAY_MONTH_RE.sub(lambda m: iso(m.group(2), m.group(1), m.group(0)), text)


def parse_when(when: str, now: Optional[datetime] = None) -> tuple[Optional[datetime], bool, str]:
    """(start, all_day, error). "tomorrow 3pm", "3pm on friday", "2026-10-20 15:00", "in 2 hours";
    a day alone ("friday", "2026-10-20", "sept 19") is an all-day event."""
    from app.agent.tools.reminders import parse_when as clock

    now = now or _now()
    text = " ".join(str(when or "").lower().replace(",", " ").split())
    text = re.sub(r"(\d{4}-\d{2}-\d{2})t(?=\d)", r"\1 ", text)
    text = _spell_date(text, now)
    bad = (None, False, f"Error: couldn't read the time '{when}'. Use e.g. 'tomorrow 3pm', 'friday 10:00', "
                        "'2026-10-20 15:00', or a day alone for all day.")
    if not text:
        return None, False, "Error: when? Give a day and time, e.g. 'tomorrow 3pm'."
    day, rest = None, text
    m = _DAY_RE.search(text)
    if m:
        try:
            day = _day(m.group(1), now)
        except ValueError:
            return bad
        rest = text[:m.start()] + " " + text[m.end():]
    rest = " ".join(re.sub(r"\b(?:on|at|from|this)\b", " ", rest).split())
    if not rest:
        return (datetime.combine(day, datetime.min.time()), True, "") if day else bad
    at = clock(rest, datetime.combine(day, datetime.min.time()) if day else now)
    if at is None or (day is not None and at.date() != day):
        return bad
    return at.replace(second=0, microsecond=0), False, ""


_DURATION_RE = re.compile(r"(?:(\d+(?:\.\d+)?)\s*h(?:ours?|rs?)?)?\s*(?:(\d+)\s*m(?:in(?:ute)?s?)?)?")


def parse_duration(text: str) -> tuple[Optional[timedelta], str]:
    """'1h', '30m', '1h30m', '90 minutes', '2 hours'; '' = None (the caller's default)."""
    t = " ".join(str(text or "").lower().replace("half an hour", "30m").replace("an hour", "1h").split())
    if not t:
        return None, ""
    if t.isdigit():
        return timedelta(minutes=int(t)), ""
    m = _DURATION_RE.fullmatch(t)
    if not m or not (m.group(1) or m.group(2)):
        return None, f"Error: couldn't read the duration '{text}'. Use e.g. '1h', '30m' or '1h30m'."
    span = timedelta(hours=float(m.group(1) or 0), minutes=int(m.group(2) or 0))
    return (span, "") if span > timedelta(0) else (None, "Error: the duration must be longer than zero.")


_SPAN_RE = re.compile(r"next (\d{1,3}) (day|week|month)s?")


def _agenda_range(when: str, now: datetime) -> tuple[Optional[datetime], int, str]:
    # Device 2026-10-19: "check for the whole year" became when='this week' -- longer spans are read here.
    t = " ".join(re.sub(r"\b(?:for|the|whole|entire|rest of|of|all)\b", " ", str(when or "").lower()).split())
    today = datetime.combine(now.date(), datetime.min.time())
    if t in ("", "week", "this week", "upcoming", "coming up", "soon", "next few days"):
        return today, 7, ""
    if t == "next week":
        return today + timedelta(days=7 - now.weekday()), 7, ""
    if t in ("month", "this month", "next month"):
        return today, 31, ""
    if t in ("year", "this year", "next year", "everything", "anything"):
        return today, 365, ""
    m = _SPAN_RE.fullmatch(t)
    if m:
        return today, min(365, int(m.group(1)) * {"day": 1, "week": 7, "month": 31}[m.group(2)]), ""
    at, _, err = parse_when(t, now)
    if at is None:
        return None, 0, (f"Error: couldn't read '{when}' for the agenda. Use 'today', 'tomorrow', a weekday, "
                         "'2026-10-20', 'this week', 'this month' or 'this year'.")
    return datetime.combine(at.date(), datetime.min.time()), 1, ""


# --- finding events ---------------------------------------------------------------------------

def _live(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in events if not e.get("deleted")]


def _words_hit(ev: dict[str, Any], key: str) -> bool:
    hay = f"{ev['title']} {ev.get('location', '')}".lower()
    words = re.findall(r"\w+", key)
    return key in hay or bool(words) and all(w in hay for w in words)


def _match(events: list[dict[str, Any]], key: str, now: datetime) -> tuple[Optional[dict[str, Any]], str]:
    """An event by id, else by words of its title -- upcoming ones first; one match or an error."""
    k = " ".join(str(key or "").split()).lower()
    if not k:
        return None, "Error: which event? Give event (its id from the agenda, or some of its words)."
    live = _live(events)
    by_id = [e for e in live if e["id"].lower() == k]
    if len(by_id) == 1:
        return by_id[0], ""
    for pool in ([e for e in live if end_dt(e) > now], live):
        hits = [e for e in pool if _words_hit(e, k)]
        if len(hits) == 1:
            return hits[0], ""
        if hits:
            hits.sort(key=start_dt)
            return None, "Error: several events match: " + "; ".join(label(e) for e in hits[:6]) + ". Use the id."
    return None, f"Error: no event matches '{key}'. calendar(action='agenda') or action='find' shows them with ids."


# --- the tool ---------------------------------------------------------------------------------

def calendar(action: str = "agenda", title: str = "", when: str = "", duration: str = "", event: str = "",
             repeat: str = "", session_id: Optional[str] = None) -> str:
    """
    The user's calendar (synced with Google Calendar): the agenda for a day or week, or find, add, move or delete an event.

    Args:
        action: agenda, find, add, move or delete.
        title: What the event is (add), or its new name (move).
        when: When (add, move): 'tomorrow 3pm', 'friday 10:00', '2026-10-20 15:00'; a day alone = all day, e.g. '2026-09-19' or 'sept 19'. For agenda: 'today', 'tomorrow', 'friday', 'this week', 'this month' or 'this year'.
        duration: How long (add, move): '1h', '30m', '1h30m'. Default 1 hour.
        event: Which event (move, delete): its id from the agenda, or some of its words. For find: the words to look for.
        repeat: 'yearly' (add) for a birthday or anniversary that comes back every year; empty = once.
    """
    act = str(action or "agenda").strip().lower()
    if act not in ("agenda", "find", "add", "move", "delete"):
        return "Error: action must be agenda, find, add, move or delete."
    from app.google.calendar_sync import maybe_sync, sync_soon

    maybe_sync()  # Google's side first, when signed in (at most once a minute; never raises)
    out = _calendar(act, title, when, duration, event, repeat, session_id)
    if act in ("add", "move", "delete") and not out.startswith(("Error", "Not saved", "Already")):
        sync_soon()  # and this change to Google, in the background
    return out


_REPEATS = {"": "", "yearly": "yearly", "every year": "yearly", "annually": "yearly", "year": "yearly"}


def _calendar(act: str, title: str, when: str, duration: str, event: str, repeat: str,
              session_id: Optional[str]) -> str:
    now = _now()
    with LOCK:
        state = load()
        events: list[dict[str, Any]] = state["events"]
        key = " ".join(str(event or title or "").split()).lower()
        if act == "find" and (not key and str(when or "").strip() or key and _agenda_range(key, now)[0]):
            # find(when='tomorrow') or find(event='tomorrow') (eval 2026-10-19): the agenda for that day
            act, when = "agenda", when if str(when or "").strip() else key
        if act == "agenda":
            start, days, err = _agenda_range(when, now)
            if err:
                return err
            end = start + timedelta(days=days)
            hits = _in_range(events, start, end)
            fmt = "%a %d %b %Y" if days > 31 else "%a %d %b"
            span = f"{start:%a %d %b}" if days == 1 else f"{start:{fmt}} - {end - timedelta(days=1):{fmt}}"
            if not hits:
                # Device 2026-10-19: after "nothing this week" the model said birthdays weren't stored at all.
                later = _in_range(events, end, end + timedelta(days=_LOOKAHEAD_DAYS))
                return f"Nothing on the calendar for {span}." + (f" The next event is {label(later[0])}." if later else "")
            more = f"\n(and {len(hits) - _LIST_MAX} more)" if len(hits) > _LIST_MAX else ""
            return f"{span}:\n" + "\n".join(f"- {label(e)}" for e in hits[:_LIST_MAX]) + more
        if act == "find":
            if not key:
                return "Error: find needs event (the words to look for)."
            hits = [e for e in _live(events) if _words_hit(e, key)]
            if not hits:
                return f"No event matches '{key}'."
            upcoming = sorted((e for e in hits if end_dt(e) > now), key=start_dt)
            past = sorted((e for e in hits if end_dt(e) <= now), key=start_dt, reverse=True)
            lines = [f"- {label(e)}" for e in upcoming[:10]] + [f"- {label(e)} (past)" for e in past[:5]]
            return f"{len(hits)} found:\n" + "\n".join(lines)

        if _is_ephemeral(session_id):
            return _OFF_RECORD
        if act == "add":
            name = " ".join(str(title or "").split())
            if not name:
                return "Error: add needs the event's title."
            at, all_day, err = parse_when(when, now)
            if err:
                return err
            span, err = parse_duration(duration)
            if err:
                return err
            if str(repeat or "").strip().lower() not in _REPEATS:
                return f"Error: repeat must be 'yearly' or empty, not '{repeat}'."
            end = at + (timedelta(days=1) if all_day else span or timedelta(hours=1))
            ev = {"id": new_id(events), "title": name, "start": _stamp(at, all_day), "end": _stamp(end, all_day),
                  "location": "", "gid": "", "dirty": True, "updated": time.time()}
            if _REPEATS[str(repeat or "").strip().lower()]:
                ev["repeat"] = "yearly"
            dup = next((e for e in _live(events) if e["title"].lower() == name.lower() and e["start"] == ev["start"]), None)
            if dup is not None:
                return f"Already on the calendar: {label(dup)}."
            events.append(ev)
            save(state)
            past = " (That time has already passed.)" if not all_day and at < now else ""
            return f"Added {label(ev)}.{past}"

        ev, err = _match(events, event, now)
        if ev is None:
            return err
        if act == "delete":
            if ev.get("gid"):
                ev.update(deleted=True, dirty=True, updated=time.time())
            else:
                events.remove(ev)
            save(state)
            return f"Deleted: {label(ev)}."
        # move
        span, err = parse_duration(duration)
        if err:
            return err
        if not str(when or "").strip() and span is None and not str(title or "").strip():
            return "Error: move needs when (the new time), duration or a new title."
        old_start, old_len, was_all_day = start_dt(ev), end_dt(ev) - start_dt(ev), is_all_day(ev)
        at, all_day = old_start, was_all_day
        if str(when or "").strip():
            at, all_day, err = parse_when(when, now)
            if err:
                return err
        if all_day:
            length = old_len if was_all_day else timedelta(days=1)
        else:
            length = span or (old_len if not was_all_day else timedelta(hours=1))
        ev.update(start=_stamp(at, all_day), end=_stamp(at + length, all_day), dirty=True, updated=time.time())
        if str(title or "").strip():
            ev["title"] = " ".join(str(title).split())
        save(state)
        return f"Moved: {label(ev)}."


def upcoming(within: timedelta, now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """Timed events starting from now to now + within (for the "X in 10 minutes" nudge)."""
    now = now or _now()
    with LOCK:
        events = _live(load()["events"])
    return sorted((e for e in events if not is_all_day(e) and now <= start_dt(e) <= now + within), key=start_dt)
