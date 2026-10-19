"""PLAN 4.16 phase C: the day briefing -- composed in code, once a day on the first open (from 4 AM)."""
from datetime import date, datetime

import pytest

from app.agent.tools import calendar_events as ce
from app.agent.tools import notes as nm
from app.awareness import day_briefing as db
from app.config import settings
from app.google import auth

NOW = datetime(2026, 10, 20, 8, 15)  # a Tuesday morning


class Persona:
    address_term = "sir"


class FakeMonitor:
    def __init__(self, subscribers=True, active=()):
        self.has_subscribers = subscribers
        self._active = list(active)
        self.emitted = []

    def active_observations(self):
        return self._active

    def emit(self, obs):
        self.emitted.append(obs)
        return obs


@pytest.fixture
def day(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "notes_dir", str(tmp_path / "notes"))
    monkeypatch.setattr(auth, "DATA_DIR", tmp_path / "google")
    monkeypatch.setenv("JARVIS_BRIEFING_STATE", str(tmp_path / "briefing.json"))
    nm.notes_dir().mkdir(parents=True)
    (nm.notes_dir() / "tasks.md").write_text(
        "# Tasks\n\n- [ ] Pay rent (due 2026-10-20) <!-- id:t0001 -->\n"
        "- [ ] Return library book (due 2026-10-17) <!-- id:t0002 -->\n"
        "- [ ] Call mum <!-- id:t0003 -->\n- [x] Buy milk (done 2026-10-19) <!-- id:t0004 -->\n", encoding="utf-8")
    ev = lambda i, t, s, e: {"id": f"e000{i}", "title": t, "start": s, "end": e, "location": "", "gid": "",
                             "dirty": False, "updated": 0}
    ce.save({"events": [ev(1, "Lunch with Sam", "2026-10-20T13:00", "2026-10-20T14:00"),
                        ev(2, "Dentist", "2026-10-20T16:30", "2026-10-20T17:00"),
                        ev(3, "Mum's birthday", "2026-10-20", "2026-10-21"),
                        ev(4, "Girija Anni birthday", "2026-11-11T06:00", "2026-11-11T07:00"),
                        ev(5, "Yesterday's thing", "2026-10-19T10:00", "2026-10-19T11:00")]})
    return tmp_path


def test_a_full_day(day):
    out = db.compose(Persona(), NOW, db.gather(NOW))
    assert out["title"] == "Good morning, sir."
    text = out["text"]
    assert text.splitlines()[0] == "Tuesday 20 October."
    assert "All day: Mum's birthday" in text and "13:00 Lunch with Sam" in text and "16:30 Dentist" in text
    assert "Due today: Pay rent" in text and "Overdue: Return library book (due 2026-10-17)" in text
    assert "1 other open task on the list." in text and "Next: Wed 11 Nov 06:00 Girija Anni birthday" in text
    assert "Yesterday's thing" not in text and "Buy milk" not in text and "**" not in text
    spoken = out["spoken"]
    assert spoken.startswith("Good morning, sir. It's Tuesday the 20th of October.")
    assert "Lunch with Sam at 1 PM, Dentist at 4:30 PM and Mum's birthday" in spoken
    assert "Due today: Pay rent." in spoken and "Overdue: Return library book." in spoken
    assert "watching" not in spoken.lower()


def test_an_empty_day_names_whats_next_and_the_machine_only_when_watched(day):
    (nm.notes_dir() / "tasks.md").write_text("# Tasks\n", encoding="utf-8")
    later = datetime(2026, 10, 25, 19, 0)
    out = db.compose(Persona(), later, db.gather(later), conditions=["vram_high"])
    assert out["title"] == "Good evening, sir."
    assert "Nothing on the calendar today." in out["text"] and "Due today" not in out["text"]
    assert "Nothing is due today." in out["spoken"]
    assert "Next up is Girija Anni birthday on Wednesday the 11th of November." in out["spoken"]
    assert "Watching: vram high" in out["text"] and "I'm watching vram high" in out["spoken"]


def test_the_day_starts_at_4_am():
    assert db.briefing_day(datetime(2026, 10, 20, 1, 30)) == date(2026, 10, 19)
    assert db.briefing_day(datetime(2026, 10, 20, 4, 0)) == date(2026, 10, 20)


@pytest.mark.asyncio
async def test_once_a_day_and_only_with_the_app_open(day):
    refreshed = []
    closed = FakeMonitor(subscribers=False)
    assert await db.brief_if_due(closed, Persona, NOW, lambda: refreshed.append(1)) is None
    assert refreshed == [] and db.last_briefed() == ""

    m = FakeMonitor()
    obs = await db.brief_if_due(m, Persona, NOW, lambda: refreshed.append(1))
    assert obs is not None and obs.kind == "briefing:2026-10-20" and refreshed == [1]
    assert obs.data == {"events": 3, "due": 1, "overdue": 1}
    assert await db.brief_if_due(m, Persona, datetime(2026, 10, 20, 15, 0), lambda: None) is None  # a restart later
    assert await db.brief_if_due(m, Persona, datetime(2026, 10, 21, 2, 0), lambda: None) is None   # still "today"
    assert await db.brief_if_due(m, Persona, datetime(2026, 10, 21, 9, 0), lambda: None) is not None
    assert len(m.emitted) == 2


@pytest.mark.asyncio
async def test_a_failed_refresh_still_briefs(day):
    def offline():
        raise OSError("offline")
    assert (await db.brief_if_due(FakeMonitor(), Persona, NOW, offline)).title == "Good morning, sir."


def test_a_scheduled_briefing_routine_is_the_day_briefing(day):
    from app.routines.models import Routine, RoutineKind
    from app.routines.scheduler import RoutineScheduler

    sched = RoutineScheduler(monitor=FakeMonitor(), persona_provider=Persona, state_path=day / "routines.json")
    obs = sched._build_observation(Routine(name="Morning", time="08:00", kind=RoutineKind.BRIEFING))
    assert obs.kind.startswith("routine:") and obs.data["routine_id"]
    # the real clock (the scheduler's), so only the shape: a greeting, the date line, the day's tasks
    assert obs.title.startswith("Good ") and obs.detail.splitlines()[0].endswith(".")
    assert "Return library book" in obs.detail and "GPU" not in obs.detail
