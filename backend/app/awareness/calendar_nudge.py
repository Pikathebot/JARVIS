"""'Dentist in 10 minutes' (PLAN 4.16 phase B): a timed calendar event about to start is announced
once through the awareness channel, the way a reminder is (shown and spoken by the client, and a
Windows toast when Jarvis isn't in front). A moved event is announced again for its new time."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from app.awareness.observations import Observation, Severity

logger = logging.getLogger("jarvis.awareness.calendar")

LEAD = timedelta(minutes=10)
_announced: dict[str, str] = {}  # event id -> the start it was announced for


def nudge_once(monitor: Any, now: Optional[datetime] = None) -> list[Observation]:
    from app.agent.tools import calendar_events as ce
    from app.routines.scheduler import _toast_unless_jarvis_in_front

    now = now or datetime.now()
    out = []
    for ev in ce.upcoming(LEAD, now):
        if _announced.get(ev["id"]) == ev["start"]:
            continue
        _announced[ev["id"]] = ev["start"]
        mins = max(1, round((ce.start_dt(ev) - now).total_seconds() / 60))
        where = f", at {ev['location']}" if ev.get("location") else ""
        text = f"{ev['title']} in {mins} minute{'s' if mins != 1 else ''}{where}."
        out.append(monitor.emit(Observation(
            kind=f"calendar:{ev['id']}", severity=Severity.INFO, title=ev["title"], detail=text, spoken=text,
            data={"event_id": ev["id"], "start": ev["start"]})))
        _toast_unless_jarvis_in_front(ev["title"], text)
    return out
