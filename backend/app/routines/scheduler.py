"""
The routine scheduler.

Polls the wall clock, not a cron daemon: every `check_seconds` it asks "is it
this routine's minute, today, and have I not already fired it today?" That
makes missed ticks (the process was asleep, the loop was slow) self-healing —
a routine either fires once during its minute or is skipped for the day,
never double-fires and never needs a separate persistent job queue.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from app.awareness.briefing import build_briefing
from app.awareness.monitor import AwarenessMonitor
from app.awareness.observations import Observation, Severity
from app.config import BASE_DIR
from app.routines.models import Routine, RoutineKind

logger = logging.getLogger("jarvis.routines")

DEFAULT_STATE_PATH = BASE_DIR.parent / "data" / "routines.json"
# A one-shot reminder this far past its minute was missed (Jarvis off or asleep): say so.
LATE_AFTER_SECONDS = 180


def _toast_unless_jarvis_in_front(title: str, message: str) -> None:
    """A Windows toast for a reminder, unless the Jarvis window is already in front (the client
    shows and speaks it there)."""
    try:
        import ctypes
        import psutil

        user32 = ctypes.windll.user32
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), ctypes.byref(pid))
        if pid.value and psutil.Process(pid.value).name().lower() == "jarvis.app.exe":
            return
    except Exception:
        pass
    try:
        from app.agent.tools.notify import send_toast
        send_toast(title, message)
    except Exception as e:
        logger.warning("Reminder toast failed: %s", e)


class RoutineScheduler:
    """Fires time-of-day routines through the awareness monitor's channel."""

    def __init__(
        self,
        monitor: AwarenessMonitor,
        persona_provider: Callable[[], Any],
        state_path: Optional[Path] = None,
        check_seconds: float = 20.0,
    ):
        self.monitor = monitor
        self.persona_provider = persona_provider
        self.state_path = Path(state_path) if state_path else DEFAULT_STATE_PATH
        self.check_seconds = check_seconds
        self.enabled = True

        self._routines: dict[str, Routine] = {}
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._load_state()

    # ---------------------------------------------------------------- state

    def _load_state(self) -> None:
        try:
            if self.state_path.exists():
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                for item in data.get("routines", []):
                    routine = Routine.from_dict(item)
                    self._routines[routine.id] = routine
        except Exception as e:
            logger.warning("Could not read routine state from %s: %s", self.state_path, e)

    def _save_state(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps(
                    {"routines": [r.to_dict() for r in self._routines.values()]},
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("Could not persist routine state to %s: %s", self.state_path, e)

    # -------------------------------------------------------------- CRUD

    def list(self) -> list[Routine]:
        # One-shot reminders first (by date), then the daily schedule (by time of day).
        return sorted(self._routines.values(), key=lambda r: (r.at is None, r.at or r.time))

    def get(self, routine_id: str) -> Optional[Routine]:
        return self._routines.get(routine_id)

    def create(self, routine: Routine) -> Routine:
        self._routines[routine.id] = routine
        self._save_state()
        return routine

    def update(self, routine_id: str, **fields: Any) -> Optional[Routine]:
        routine = self._routines.get(routine_id)
        if routine is None:
            return None
        for key, value in fields.items():
            if value is not None and hasattr(routine, key):
                setattr(routine, key, value)
        self._save_state()
        return routine

    def delete(self, routine_id: str) -> bool:
        if routine_id not in self._routines:
            return False
        del self._routines[routine_id]
        self._save_state()
        return True

    # ------------------------------------------------------------- firing

    def _build_observation(self, routine: Routine) -> Observation:
        persona = self.persona_provider()

        if routine.kind == RoutineKind.MESSAGE:
            text = routine.message.strip() or routine.name
            return Observation(
                kind=f"routine:{routine.id}",
                severity=Severity.INFO,
                title=routine.name,
                detail=text,
                spoken=text,
                data={"routine_id": routine.id},
            )

        snapshot = self.monitor.last_snapshot or self.monitor.collect_snapshot()
        payload = build_briefing(
            snapshot=snapshot,
            persona=persona,
            active_conditions=self.monitor.active_observations(),
        )
        return Observation(
            kind=f"routine:{routine.id}",
            severity=Severity.INFO,
            title=routine.name,
            detail=payload["text"],
            spoken=payload["spoken"],
            data={"routine_id": routine.id},
        )

    def fire(self, routine: Routine, late_since: Optional[str] = None) -> Observation:
        """Fire a routine immediately, independent of scheduling."""
        observation = self._build_observation(routine)
        if late_since:
            observation.detail = f"(Due at {late_since}, while Jarvis was off.) {observation.detail}"
        emitted = self.monitor.emit(observation)
        if routine.reminder:
            _toast_unless_jarvis_in_front(routine.name, routine.message or routine.name)
        return emitted

    def _due(self, routine: Routine, now: datetime) -> bool:
        if not routine.enabled:
            return False
        if routine.at:
            return now.strftime("%Y-%m-%dT%H:%M") >= routine.at
        today = now.strftime("%Y-%m-%d")
        if routine.last_fired_date == today:
            return False
        if routine.days and now.weekday() not in routine.days:
            return False
        return now.strftime("%H:%M") == routine.time

    async def _check_once(self) -> None:
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        for routine in list(self._routines.values()):
            if not self._due(routine, now):
                continue
            late = None
            if routine.at:
                due = datetime.strptime(routine.at, "%Y-%m-%dT%H:%M")
                if (now - due).total_seconds() > LATE_AFTER_SECONDS:
                    late = due.strftime("%H:%M on %a %d %b")
            try:
                self.fire(routine, late_since=late)
            except Exception as e:
                logger.warning("Routine %s failed to fire: %s", routine.id, e)
            routine.last_fired_date = today
            if routine.at:
                # One-shot: done. (A failed fire is not retried every 20 s forever.)
                self._routines.pop(routine.id, None)
        self._save_state()

    # ----------------------------------------------------------- lifecycle

    async def _loop(self) -> None:
        logger.info("Routine scheduler started (check=%.0fs)", self.check_seconds)
        while self._running:
            try:
                if self.enabled:
                    await self._check_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Routine check failed: %s", e)
            await asyncio.sleep(self.check_seconds)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        logger.info("Routine scheduler stopped")

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "running": self._running,
            "check_seconds": self.check_seconds,
            "count": len(self._routines),
        }
