"""remind_me / reminders (tool review batch 4, 2026-10-01): reminders from chat are routines on
the scheduler -- survive restarts, fire once through the awareness channel, late if missed."""
import asyncio
from datetime import datetime

import pytest

from app.agent.tools import reminders as rem
from app.routines import scheduler as sched_mod
from app.routines.scheduler import RoutineScheduler

NOW = datetime(2026, 10, 1, 14, 0)  # a Thursday


class Monitor:
    def __init__(self):
        self.emitted = []
        self.last_snapshot = None

    def emit(self, observation):
        self.emitted.append(observation)
        return observation


@pytest.fixture
def scheduler(tmp_path, monkeypatch):
    s = RoutineScheduler(monitor=Monitor(), persona_provider=lambda: None, state_path=tmp_path / "routines.json")
    monkeypatch.setattr(rem, "_scheduler", lambda: s)
    monkeypatch.setattr(rem, "_now", lambda: NOW)
    toasts = []
    monkeypatch.setattr(sched_mod, "_toast_unless_jarvis_in_front", lambda title, msg: toasts.append(msg))
    s.toasts = toasts
    return s


@pytest.mark.parametrize("when, expected", [
    ("in 20 minutes", datetime(2026, 10, 1, 14, 20)),
    ("in 1 hour 30 minutes", datetime(2026, 10, 1, 15, 30)),
    ("in 2h", datetime(2026, 10, 1, 16, 0)),
    ("17:30", datetime(2026, 10, 1, 17, 30)),
    ("5pm", datetime(2026, 10, 1, 17, 0)),
    ("9am", datetime(2026, 10, 2, 9, 0)),          # already past today -> tomorrow
    ("tomorrow 9:30am", datetime(2026, 10, 2, 9, 30)),
    ("tomorrow at 18:00", datetime(2026, 10, 2, 18, 0)),
    ("2026-10-05 08:00", datetime(2026, 10, 5, 8, 0)),
    ("2026-10-05T08:00", datetime(2026, 10, 5, 8, 0)),
])
def test_parse_when(when, expected):
    assert rem.parse_when(when, now=NOW) == expected


@pytest.mark.parametrize("when", ["", "soon", "25:00", "13pm", "in minutes"])
def test_unreadable_times_are_refused(when):
    assert rem.parse_when(when, now=NOW) is None


def test_set_list_and_cancel(scheduler):
    out = rem.remind_me("Stretch your legs", "in 20 minutes")
    assert out.startswith("Reminder set (") and "14:20 on Thu 01 Oct" in out
    rem.remind_me("Stand up", "9am", repeat="weekdays")
    listed = rem.reminders()
    assert "Stretch your legs" in listed and "every weekday at 09:00" in listed

    rid = scheduler.list()[0].id
    assert rem.reminders("cancel", rid).startswith("Cancelled")
    assert len([r for r in scheduler.list() if r.reminder]) == 1
    assert rem.reminders("cancel", "nope").startswith("Error")
    # Restart-safe: a new scheduler on the same file sees what is left.
    again = RoutineScheduler(monitor=Monitor(), persona_provider=lambda: None, state_path=scheduler.state_path)
    assert [r.message for r in again.list()] == ["Stand up"]


def test_bad_input_is_refused(scheduler):
    assert rem.remind_me("", "in 5 minutes").startswith("Error")
    assert rem.remind_me("x", "whenever").startswith("Error: Couldn't read the time")
    assert rem.remind_me("x", "2026-09-30 10:00").startswith("Error") and "already passed" in rem.remind_me("x", "2026-09-30 10:00")
    assert rem.remind_me("x", "5pm", repeat="hourly").startswith("Error")


def test_a_one_shot_fires_once_then_is_gone_and_says_when_late(scheduler, monkeypatch):
    rem.remind_me("Call the dentist", "in 20 minutes")
    rem.remind_me("Water the plants", "in 30 minutes")

    class Clock(datetime):
        current = datetime(2026, 10, 1, 14, 20, 5)

        @classmethod
        def now(cls, tz=None):
            return cls.current

    monkeypatch.setattr(sched_mod, "datetime", Clock)
    asyncio.run(scheduler._check_once())
    assert [o.detail for o in scheduler.monitor.emitted] == ["Call the dentist"]
    assert scheduler.toasts == ["Call the dentist"]
    asyncio.run(scheduler._check_once())
    assert len(scheduler.monitor.emitted) == 1  # never twice

    # Jarvis was off at 14:30; it fires on the next check, saying it is late.
    Clock.current = datetime(2026, 10, 1, 19, 0)
    asyncio.run(scheduler._check_once())
    late = scheduler.monitor.emitted[-1]
    assert late.detail.startswith("(Due at 14:30") and "Water the plants" in late.detail
    assert [r for r in scheduler.list() if r.reminder] == []
