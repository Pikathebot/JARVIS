"""
Scheduled routines: things Jarvis says at a time of day rather than in
response to a hardware condition.

A routine is deliberately dumb — a time, a set of weekdays, and either the
deterministic briefing or a fixed message. There is no NLP scheduling here;
"every weekday at 8am" is a checkbox row, not a parsed sentence.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class RoutineKind(str, Enum):
    BRIEFING = "briefing"
    MESSAGE = "message"


@dataclass
class Routine:
    """A time-triggered announcement."""

    name: str
    time: str  # "HH:MM", 24-hour, local time
    kind: RoutineKind = RoutineKind.BRIEFING
    message: str = ""  # spoken text, only used when kind == MESSAGE
    # Weekday numbers 0 (Monday) - 6 (Sunday); empty means every day.
    days: list[int] = field(default_factory=list)
    enabled: bool = True
    id: str = field(default_factory=lambda: f"rtn_{uuid.uuid4().hex[:12]}")
    # "YYYY-MM-DD" of the last local date this routine fired, so a restart
    # or a slow poll tick cannot fire it twice in one day.
    last_fired_date: Optional[str] = None
    # One-shot: "YYYY-MM-DDTHH:MM" local. Fires once when that minute has come (late, if Jarvis
    # was off then) and is then removed. None = the daily ``time`` schedule above.
    at: Optional[str] = None
    # Set by remind_me from chat: also raises a Windows toast when Jarvis isn't in front.
    reminder: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "time": self.time,
            "kind": self.kind.value,
            "message": self.message,
            "days": list(self.days),
            "enabled": self.enabled,
            "last_fired_date": self.last_fired_date,
            "at": self.at,
            "reminder": self.reminder,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Routine":
        return cls(
            id=str(data.get("id") or f"rtn_{uuid.uuid4().hex[:12]}"),
            name=str(data.get("name", "")),
            time=str(data.get("time", "08:00")),
            kind=RoutineKind(data.get("kind", "briefing")),
            message=str(data.get("message", "")),
            days=[int(d) for d in data.get("days", []) if isinstance(d, (int, float, str)) and str(d).strip() != ""],
            enabled=bool(data.get("enabled", True)),
            last_fired_date=data.get("last_fired_date"),
            at=data.get("at") or None,
            reminder=bool(data.get("reminder", False)),
        )
