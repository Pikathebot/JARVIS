"""The day briefing (PLAN 4.16 phase C): the first time Jarvis is open each day it says what the day
holds -- today's events, tasks due today or overdue, the next event after today -- and mentions the
machine only when the monitor is watching a problem. The user has no fixed start time (2026-10-19:
"the first time when i launch jarvis"), so it fires on the first open of a day rather than at an hour.

Composed in code like the status briefing (``briefing.py``): instant, available with the model
evicted, nothing invented. A day starts at 4 AM, so a 1 AM session still belongs to the day before.
The last briefed day lives in ``data/briefing.json`` (``JARVIS_BRIEFING_STATE`` overrides it; the
tests do) so a restart later the same day stays quiet.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from app.awareness.briefing import address_suffix
from app.awareness.observations import Observation, Severity
from app.config import BASE_DIR

logger = logging.getLogger("jarvis.awareness.day_briefing")

DAY_STARTS_AT = timedelta(hours=4)
_LIST_MAX = 6


def state_path() -> Path:
    return Path(os.environ.get("JARVIS_BRIEFING_STATE") or BASE_DIR.parent / "data" / "briefing.json")


def briefing_day(now: datetime) -> date:
    return (now - DAY_STARTS_AT).date()


def last_briefed() -> str:
    try:
        return json.loads(state_path().read_text(encoding="utf-8")).get("last_day", "")
    except (OSError, ValueError):
        return ""


def mark_briefed(day: date) -> None:
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"last_day": day.isoformat()}), encoding="utf-8")
    tmp.replace(p)


# --- what the day holds -------------------------------------------------------------------------

def _clock(at: datetime) -> str:
    """'2 PM', '2:30 PM' -- for speech."""
    h = at.strftime("%I").lstrip("0")
    return f"{h} {at:%p}" if at.minute == 0 else f"{h}:{at:%M %p}"


def _spoken_day(d: date) -> str:
    n = d.day
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{d:%A} the {n}{suffix} of {d:%B}"


def gather(now: datetime) -> dict[str, Any]:
    """Today's events (timed, all-day), the next event after today, and the open tasks by due date."""
    from app.agent.tools import calendar_events as ce
    from app.agent.tools import notes as nm

    day = briefing_day(now)
    start = datetime.combine(day, datetime.min.time())
    end = start + timedelta(days=1)
    with ce.LOCK:
        events = ce._live(ce.load()["events"])
    today = sorted((e for e in events if ce.start_dt(e) < end and ce.end_dt(e) > start), key=ce.start_dt)
    later = sorted((e for e in events if ce.start_dt(e) >= end), key=ce.start_dt)
    try:
        with nm.TASKS_LOCK:
            _, lines = nm._read_tasks()
            tasks = [t for t in nm._parse_tasks(lines) if not t.done]
    except OSError:
        tasks = []
    iso = day.isoformat()
    from app.agent.tools.gmail import unread_summary

    return {
        "mail": unread_summary(),  # None when Gmail isn't connected or allowed
        "day": day,
        "timed": [e for e in today if not ce.is_all_day(e)],
        "all_day": [e for e in today if ce.is_all_day(e)],
        "next": later[0] if later else None,
        "due_today": [t for t in tasks if t.due == iso],
        "overdue": sorted((t for t in tasks if t.due and t.due < iso), key=lambda t: t.due),
        "other_open": [t for t in tasks if not t.due or t.due > iso],
    }


def compose(persona: Any, now: datetime, day: dict[str, Any],
            conditions: Optional[list[str]] = None) -> dict[str, str]:
    """{"title", "text", "spoken"}: plain lines for the awareness tray (no markdown), and speech."""
    from app.agent.tools import calendar_events as ce

    hour = now.hour
    greeting = ("Good morning" if 4 <= hour < 12 else "Good afternoon" if hour < 18 else "Good evening")
    title = f"{greeting}{address_suffix(persona)}."
    d: date = day["day"]
    lines = [f"{d:%A %d %B}."]
    spoken = [title, f"It's {_spoken_day(d)}."]

    timed, all_day = day["timed"], day["all_day"]
    for e in all_day[:_LIST_MAX]:
        lines.append(f"All day: {e['title']}")
    for e in timed[:_LIST_MAX]:
        lines.append(f"{ce.start_dt(e):%H:%M} {e['title']}" + (f" @ {e['location']}" if e.get("location") else ""))
    if timed or all_day:
        said = [f"{e['title']} at {_clock(ce.start_dt(e))}" for e in timed[:3]] + [e["title"] for e in all_day[:2]]
        spoken.append(f"Today you have {_join(said)}." if said else "")
    else:
        lines.append("Nothing on the calendar today.")
        spoken.append("Nothing is on the calendar today.")

    due, overdue, other = day["due_today"], day["overdue"], day["other_open"]
    if due:
        lines.append("Due today: " + "; ".join(t.text + (f" ({t.due_time})" if t.due_time else "") for t in due[:_LIST_MAX]))
        spoken.append(f"Due today: {_join([t.text for t in due[:3]])}.")
    if overdue:
        lines.append("Overdue: " + "; ".join(f"{t.text} (due {t.due})" for t in overdue[:_LIST_MAX]))
        spoken.append(f"Overdue: {_join([t.text for t in overdue[:3]])}.")
    if not due and not overdue:
        spoken.append("Nothing is due today.")
    if other:
        lines.append(f"{len(other)} other open task{'s' if len(other) != 1 else ''} on the list.")

    mail = day.get("mail")
    if mail is not None:
        count, more, senders = mail
        if count:
            n = f"{count}{'+' if more else ''}"
            lines.append(f"Unread in Primary: {n}" + (f" ({', '.join(senders[:4])})" if senders else ""))
            spoken.append(f"You have {n.replace('+', ' or more')} unread email{'s' if count != 1 or more else ''}"
                          + (f", from {_join(senders[:3])}." if senders else "."))
        else:
            lines.append("No unread mail in Primary.")

    nxt = day["next"]
    if nxt is not None:
        s = ce.start_dt(nxt)
        when = f"{s:%a %d %b}" + ("" if ce.is_all_day(nxt) else f" {s:%H:%M}")
        lines.append(f"Next: {when} {nxt['title']}")
        if not timed and not all_day:
            spoken.append(f"Next up is {nxt['title']} on {_spoken_day(s.date())}.")

    if conditions:
        readable = ", ".join(c.replace("_", " ") for c in conditions)
        lines.append(f"Watching: {readable}")
        spoken.append(f"Also, I'm watching {readable}.")
    return {"title": title, "text": "\n".join(lines), "spoken": " ".join(s for s in spoken if s)}


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return items[0] if items else ""
    return ", ".join(items[:-1]) + " and " + items[-1]


def build(persona: Any, now: datetime, conditions: Optional[list[str]] = None,
          kind: Optional[str] = None) -> Observation:
    day = gather(now)
    out = compose(persona, now, day, conditions)
    return Observation(kind=kind or f"briefing:{day['day'].isoformat()}", severity=Severity.INFO,
                       title=out["title"], detail=out["text"], spoken=out["spoken"],
                       data={"events": len(day["timed"]) + len(day["all_day"]), "due": len(day["due_today"]),
                             "overdue": len(day["overdue"])})


# --- the first open of the day ------------------------------------------------------------------

def _fresh_from_google() -> None:
    """Pull Google's side first (when signed in) so the briefing has this morning's changes."""
    from app.google import calendar_sync, tasks_sync

    tasks_sync.maybe_sync(0.0)
    calendar_sync.maybe_sync(0.0)


async def first_open_loop(monitor: Any, persona_provider: Callable[[], Any], check_s: float = 5.0,
                          now: Callable[[], datetime] = datetime.now,
                          refresh: Callable[[], None] = _fresh_from_google) -> None:
    """Once a day, when the app is connected (an awareness subscriber exists) and the day hasn't been
    briefed yet: sync, compose, emit, remember the day. Checks every ``check_s`` seconds, so it also
    covers a Jarvis left open overnight (the first check on the new day with the app connected)."""
    while True:
        try:
            await brief_if_due(monitor, persona_provider, now(), refresh)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Day briefing failed: %s", exc)
        await asyncio.sleep(check_s)


async def brief_if_due(monitor: Any, persona_provider: Callable[[], Any], at: datetime,
                       refresh: Callable[[], None] = _fresh_from_google) -> Optional[Observation]:
    day = briefing_day(at)
    if not monitor.has_subscribers or last_briefed() == day.isoformat():
        return None
    try:
        await asyncio.wait_for(asyncio.to_thread(refresh), timeout=20)
    except Exception as exc:  # offline or slow: brief from what's here
        logger.info("Day briefing: Google refresh skipped (%s)", exc)
    # gather reads files and asks Gmail: off the event loop
    obs = await asyncio.to_thread(build, persona_provider(), at, monitor.active_observations())
    mark_briefed(day)
    logger.info("Day briefing for %s: %s", day, obs.data)
    return monitor.emit(obs)
