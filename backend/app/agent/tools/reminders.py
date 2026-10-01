"""
Reminders from chat ("remind me in 20 minutes to stretch", "every weekday at 9 remind me to
stand up"), replacing send_toast (tool review, 2026-09-30).

They are routines on the existing scheduler, so they survive a restart (``data/routines.json``)
and fire through ``AwarenessMonitor.emit`` -- the client shows and speaks them -- plus a Windows
toast when Jarvis isn't the window in front. A one-shot reminder that came due while Jarvis was
off fires late on the next start, saying so.
"""
import re
from datetime import datetime, timedelta
from typing import Optional

from app.routines.models import Routine, RoutineKind

REPEATS = ("once", "daily", "weekdays", "weekly")


def _scheduler():
    from app.main import routine_scheduler
    return routine_scheduler


def _now() -> datetime:
    return datetime.now()


_RELATIVE_RE = re.compile(
    r"^in\s+(?:(\d+)\s*(?:h|hr|hrs|hour|hours))?\s*(?:and\s+)?(?:(\d+)\s*(?:m|min|mins|minute|minutes))?$"
)
_CLOCK_RE = re.compile(r"^(?:(today|tomorrow)\s+)?(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$")


def parse_when(when: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """"in 20 minutes", "in 1 hour 30 minutes", "17:30", "5pm", "tomorrow 9:30am",
    "2026-10-02 08:00". A bare time already past today means tomorrow. None if unreadable."""
    now = now or _now()
    text = " ".join(str(when or "").lower().replace(".", "").split())
    if not text:
        return None
    m = _RELATIVE_RE.match(text)
    if m and (m.group(1) or m.group(2)):
        return now + timedelta(hours=int(m.group(1) or 0), minutes=int(m.group(2) or 0))
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text.upper() if "T" in fmt else text, fmt)
        except ValueError:
            pass
    m = _CLOCK_RE.match(text)
    if not m:
        return None
    day, hour, minute, ampm = m.group(1), int(m.group(2)), int(m.group(3) or 0), m.group(4)
    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    if hour > 23 or minute > 59:
        return None
    at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if day == "tomorrow":
        at += timedelta(days=1)
    elif day is None and at <= now:
        at += timedelta(days=1)
    return at


def remind_me(text: str, when: str, repeat: str = "once") -> str:
    """
    Set a reminder Jarvis will say (and show) at a time: when = "in 20 minutes", "17:30", "5pm", "tomorrow 9am" or "2026-10-02 08:00"; repeat = once, daily, weekdays or weekly.

    Args:
        text: What to remind the user of, phrased to be read out ("Stretch your legs").
        when: When it should fire (local time).
        repeat: once (default), daily, weekdays or weekly.
    """
    message = " ".join(str(text or "").split())
    if not message:
        return "Error: The reminder needs some text."
    rep = str(repeat or "once").strip().lower()
    if rep not in REPEATS:
        return f"Error: repeat must be one of {', '.join(REPEATS)}."
    at = parse_when(when)
    if at is None:
        return (f"Error: Couldn't read the time '{when}'. Use e.g. 'in 20 minutes', '17:30', '5pm', "
                "'tomorrow 9am' or '2026-10-02 08:00'.")
    if rep == "once" and at <= _now():
        return f"Error: {at:%H:%M on %a %d %b} has already passed."

    name = message if len(message) <= 60 else message[:57] + "..."
    routine = Routine(name=name, time=at.strftime("%H:%M"), kind=RoutineKind.MESSAGE, message=message, reminder=True)
    if rep == "once":
        routine.at = at.strftime("%Y-%m-%dT%H:%M")
    else:
        routine.days = {"daily": [], "weekdays": [0, 1, 2, 3, 4], "weekly": [at.weekday()]}[rep]
        # Already past today's minute: the daily schedule starts tomorrow.
        if at.date() > _now().date():
            routine.last_fired_date = _now().strftime("%Y-%m-%d")
    _scheduler().create(routine)
    return f"Reminder set ({routine.id}): \"{message}\" {_describe(routine)}."


def _describe(r: Routine) -> str:
    if r.at:
        return "at " + datetime.strptime(r.at, "%Y-%m-%dT%H:%M").strftime("%H:%M on %a %d %b")
    if not r.days:
        return f"every day at {r.time}"
    if r.days == [0, 1, 2, 3, 4]:
        return f"every weekday at {r.time}"
    names = "Mon Tue Wed Thu Fri Sat Sun".split()
    return f"every {', '.join(names[d] for d in r.days)} at {r.time}"


def reminders(action: str = "list", reminder_id: str = "") -> str:
    """
    List the reminders set from chat (action="list"), or cancel one by its id (action="cancel").

    Args:
        action: "list" or "cancel".
        reminder_id: The id from the list, for cancel.
    """
    scheduler = _scheduler()
    mine = [r for r in scheduler.list() if r.reminder]
    act = str(action or "list").strip().lower()
    if act == "list":
        if not mine:
            return "No reminders are set."
        return "\n".join(f"- {r.id}: \"{r.message}\" {_describe(r)}" for r in mine)
    if act == "cancel":
        rid = str(reminder_id or "").strip()
        match = [r for r in mine if r.id == rid or (len(rid) >= 4 and r.id.endswith(rid))]
        if len(match) != 1:
            return f"Error: No single reminder matches '{reminder_id}'. List them first."
        scheduler.delete(match[0].id)
        return f"Cancelled the reminder \"{match[0].message}\" ({_describe(match[0])})."
    return "Error: action must be 'list' or 'cancel'."
