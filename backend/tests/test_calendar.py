"""PLAN 4.16 phase B: the calendar tool (local, offline) and its two-way sync with a fake Google Calendar."""
import json
import re
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import app.agent.orchestrator as orch_mod
from app.agent.permissions import RiskTier, evaluate_tool_permission
from app.agent.tools import calendar_events as ce
from app.agent.tools.registry import execute_tool
from app.google import auth, calendar_sync

NOW = datetime(2026, 10, 19, 10, 0)  # a Monday


def _iso(local: datetime) -> str:
    return local.astimezone().isoformat()


class FakeGoogleCalendar:
    def __init__(self):
        self.events: dict[str, dict] = {}
        self.n = 0
        self.time_zone = "Asia/Kolkata"

    def add(self, summary, start, end, **extra):
        self.n += 1
        gid = f"g{self.n}"
        when = (lambda d: {"date": d}) if isinstance(start, str) else (lambda d: {"dateTime": _iso(d)})
        self.events[gid] = {"id": gid, "summary": summary, "start": when(start), "end": when(end),
                            "status": "confirmed", "updated": "2026-10-19T08:00:00.000Z", **extra}
        return gid

    def touch(self, gid, **fields):
        self.events[gid].update(fields, updated=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))

    @staticmethod
    def _start(g):
        s = g["start"]
        return datetime.fromisoformat(s.get("dateTime") or s["date"] + "T00:00:00+00:00")

    def handler(self, req: httpx.Request) -> httpx.Response:
        base = "/calendar/v3/calendars/primary/events"
        if req.url.path == base and req.method == "GET":
            lo = datetime.fromisoformat(req.url.params["timeMin"])
            hi = datetime.fromisoformat(req.url.params["timeMax"])
            assert req.url.params["singleEvents"] == "true"
            return httpx.Response(200, json={"items": [g for g in self.events.values() if lo <= self._start(g) < hi],
                                              "timeZone": self.time_zone})
        if req.url.path == base and req.method == "POST":
            body = json.loads(req.content)
            if body.get("recurrence") and "dateTime" in body["start"] and not body["start"].get("timeZone"):
                # Event.start.timeZone: "For recurring events this field is required"
                return httpx.Response(400, json={"error": {"message": "Missing time zone definition for start time."}})
            self.n += 1
            gid = f"g{self.n}"
            self.events[gid] = {"id": gid, "status": "confirmed", **body}
            self.touch(gid)
            return httpx.Response(200, json=self.events[gid])
        gid = req.url.path.rsplit("/", 1)[-1]
        if gid not in self.events:
            return httpx.Response(404, json={})
        if req.method == "GET":
            return httpx.Response(200, json=self.events[gid])
        if req.method == "PATCH":
            self.touch(gid, **json.loads(req.content))
            return httpx.Response(200, json=self.events[gid])
        if req.method == "DELETE":
            del self.events[gid]
            return httpx.Response(204)
        return httpx.Response(400)


@pytest.fixture
def cal(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "DATA_DIR", tmp_path / "google")
    monkeypatch.setattr(ce, "_now", lambda: NOW)
    monkeypatch.setattr(calendar_sync, "maybe_sync", lambda *a: None)
    monkeypatch.setattr(calendar_sync, "sync_soon", lambda: None)
    return tmp_path


@pytest.fixture
def google(cal, monkeypatch):
    monkeypatch.setattr(auth, "access_token", lambda: "tok")
    monkeypatch.setattr(calendar_sync, "_time_zone", [""])
    fake = FakeGoogleCalendar()
    monkeypatch.setattr(auth, "_http", lambda: httpx.Client(transport=httpx.MockTransport(fake.handler)))
    return fake


def _events():
    return [e for e in ce.load()["events"] if not e.get("deleted")]


# --- reading times ---

@pytest.mark.parametrize("when,expected,all_day", [
    ("tomorrow 3pm", datetime(2026, 10, 20, 15, 0), False),
    ("3pm on friday", datetime(2026, 10, 23, 15, 0), False),
    ("Friday at 10:00", datetime(2026, 10, 23, 10, 0), False),
    ("2026-10-20 15:00", datetime(2026, 10, 20, 15, 0), False),
    ("2026-10-20T15:00", datetime(2026, 10, 20, 15, 0), False),
    ("in 2 hours", datetime(2026, 10, 19, 12, 0), False),
    ("friday", datetime(2026, 10, 23), True),
    ("monday", datetime(2026, 10, 26), True),  # today is Monday: the next one
    ("2026-11-02", datetime(2026, 11, 2), True),
])
def test_parse_when(when, expected, all_day):
    assert ce.parse_when(when, NOW) == (expected, all_day, "")


def test_parse_when_and_duration_errors():
    assert ce.parse_when("whenever", NOW)[2].startswith("Error")
    assert ce.parse_when("2026-13-45", NOW)[2].startswith("Error")
    assert ce.parse_duration("1h30m") == (timedelta(minutes=90), "")
    assert ce.parse_duration("90 minutes") == (timedelta(minutes=90), "")
    assert ce.parse_duration("2 hours") == (timedelta(hours=2), "")
    assert ce.parse_duration("half an hour") == (timedelta(minutes=30), "")
    assert ce.parse_duration("") == (None, "")
    assert ce.parse_duration("a while")[1].startswith("Error")


# --- the tool, locally ---

def test_add_and_agenda(cal):
    out = ce.calendar("add", title="Dentist", when="tomorrow 3pm", duration="45m")
    assert out.startswith("Added e") and "Tue 20 Oct 15:00-15:45 Dentist" in out
    ce.calendar("add", title="Mum's birthday", when="friday")
    assert "Already on the calendar" in ce.calendar("add", title="dentist", when="tomorrow 15:00")
    week = ce.calendar("agenda")
    assert "Tue 20 Oct 15:00-15:45 Dentist" in week and "Fri 23 Oct, all day Mum's birthday" in week
    assert ce.calendar("agenda", when="tomorrow").count("\n- ") == 1
    assert ce.calendar("agenda", when="today").startswith("Nothing on the calendar for Mon 19 Oct. The next event is")
    assert "has already passed" in ce.calendar("add", title="Standup", when="today 9am")


def test_move_keeps_the_length_and_delete(cal):
    ce.calendar("add", title="Dentist", when="tomorrow 3pm", duration="45m")
    assert "Wed 21 Oct 09:00-09:45 Dentist" in ce.calendar("move", event="dentist", when="wednesday 9am")
    assert "09:00-10:30" in ce.calendar("move", event="dentist", duration="1h30m")
    assert "all day" in ce.calendar("move", event="dentist", when="thursday")
    assert ce.calendar("delete", event="dentist").startswith("Deleted:")
    assert _events() == [] and ce.load()["events"] == []  # never synced: gone outright


def test_matching_by_words_and_ambiguity(cal):
    ce.calendar("add", title="Gym", when="tomorrow 7am")
    ce.calendar("add", title="Gym", when="wednesday 7am")
    assert "several events match" in ce.calendar("delete", event="gym")
    gid = _events()[0]["id"]
    assert ce.calendar("delete", event=gid).startswith("Deleted:")
    assert "no event matches" in ce.calendar("move", event="swimming", when="friday 9am")
    assert "1 found" in ce.calendar("find", event="gym")


def test_off_the_record_can_read_but_not_write(cal, monkeypatch):
    monkeypatch.setattr(ce, "_is_ephemeral", lambda sid: True)
    assert ce.calendar("add", title="Dentist", when="tomorrow 3pm", session_id="s").startswith("Not saved")
    assert ce.calendar("agenda", session_id="s").startswith("Nothing on the calendar")


def test_execute_tool_reaches_it(cal):
    out = execute_tool("calendar", {"action": "add", "title": "Dentist", "when": "tomorrow 3pm"})
    assert "Dentist" in str(out)


def test_permissions_moving_or_deleting_asks():
    assert evaluate_tool_permission("calendar", {"action": "add", "title": "x", "when": "friday"}).allowed
    assert evaluate_tool_permission("calendar", {"action": "agenda"}).allowed
    for act in ("move", "delete"):
        d = evaluate_tool_permission("calendar", {"action": act, "event": "dentist", "when": "friday 9am"})
        assert not d.allowed and d.risk_tier == RiskTier.CONFIRMATION_REQUIRED and "dentist" in d.reason
    assert not evaluate_tool_permission("calendar", {"action": "add", "title": "x"}, tainted_by="a web page").allowed
    assert evaluate_tool_permission("calendar", {"action": "agenda"}, tainted_by="a web page").allowed


def test_a_call_with_what_to_add_but_no_action_adds(cal):
    # Real-chat eval 2026-10-29, case 2: this call fell to the agenda and a birthday was never added.
    out = ce.calendar(title="Meera's Birthday", when="2026-07-22", repeat="yearly")
    assert out.startswith("Added") and "Meera" in out
    assert ce.calendar().startswith(("Mon", "Nothing"))  # nothing given is still the agenda
    assert ce.resolve_action("", title="x", when="friday", event="e12") == "agenda"  # an event named: not an add
    # and the claim check counts it as a change
    assert orch_mod.changed_something([{"tool": "calendar", "args": {"title": "x", "when": "friday"}, "status": "success"}])


def test_birthdays_and_anniversaries_come_back_every_year(cal):
    ce.calendar("add", title="Ravi's Birthday", when="2026-03-14 09:00")
    ce.calendar("add", title="Wedding anniversary", when="2027-02-14")
    ce.calendar("add", title="Dentist", when="tomorrow 3pm")
    repeats = {e["title"]: e.get("repeat") for e in _events()}
    assert repeats == {"Ravi's Birthday": "yearly", "Wedding anniversary": "yearly", "Dentist": None}


def test_reading_the_calendar_is_not_a_change():
    assert not orch_mod.changed_something([{"tool": "calendar", "args": {"action": "agenda"}, "status": "success"}])
    assert not orch_mod.changed_something([{"tool": "calendar", "args": {}, "status": "success"}])
    assert orch_mod.changed_something([{"tool": "calendar", "args": {"action": "add"}, "status": "success"}])


def test_fast_turn_and_claim_checks_know_the_calendar():
    assert orch_mod.needs_a_tool("add the dentist at 3pm tomorrow to my calendar")
    assert orch_mod.needs_a_tool("what's on my calendar today?")
    assert orch_mod.claims_unrun_action("I've added the dentist appointment to your calendar.")


# --- sync with Google ---

def test_first_sync_merges_both_sides(google):
    ce.calendar("add", title="Dentist", when="tomorrow 3pm")
    google.add("Team call", datetime(2026, 10, 21, 14, 0), datetime(2026, 10, 21, 15, 0))
    google.add("Holiday", "2026-10-26", "2026-10-28")
    n = calendar_sync.sync()
    assert n["created_there"] == 1 and n["created_here"] == 2
    dentist = next(g for g in google.events.values() if g["summary"] == "Dentist")
    assert datetime.fromisoformat(dentist["start"]["dateTime"]).astimezone().replace(tzinfo=None) == datetime(2026, 10, 20, 15)
    week = ce.calendar("agenda", when="next week")
    assert "Mon 26 Oct, all day (2 days) Holiday" in week
    assert "Wed 21 Oct 14:00-15:00 Team call" in ce.calendar("agenda")
    assert not any(calendar_sync.sync().values())  # a second sync moves nothing


def test_changes_flow_each_way(google):
    gid = google.add("Team call", datetime(2026, 10, 21, 14, 0), datetime(2026, 10, 21, 15, 0))
    ce.calendar("add", title="Dentist", when="tomorrow 3pm")
    calendar_sync.sync()
    ce.calendar("move", event="dentist", when="thursday 9am")         # moved here
    google.touch(gid, summary="Team call (moved room)", location="B2")  # renamed at Google
    n = calendar_sync.sync()
    assert n["to_google"] == 1 and n["from_google"] == 1
    dentist = next(g for g in google.events.values() if g["summary"] == "Dentist")
    assert dentist["start"]["dateTime"] == _iso(datetime(2026, 10, 22, 9, 0))
    assert "Team call (moved room) @ B2" in ce.calendar("agenda")


def test_deletions_flow_each_way_and_a_moved_away_event_is_not_deleted(google):
    a = google.add("Team call", datetime(2026, 10, 21, 14, 0), datetime(2026, 10, 21, 15, 0))
    b = google.add("Review", datetime(2026, 10, 22, 14, 0), datetime(2026, 10, 22, 15, 0))
    c = google.add("Offsite", datetime(2026, 10, 23, 9, 0), datetime(2026, 10, 23, 17, 0))
    calendar_sync.sync()
    ce.calendar("delete", event="team call")                        # deleted here
    assert "Team call" not in ce.calendar("agenda")                  # hidden while the tombstone waits
    del google.events[b]                                             # deleted at Google
    far = datetime(2027, 6, 1, 9, 0)
    google.touch(c, start={"dateTime": _iso(far)}, end={"dateTime": _iso(far + timedelta(hours=8))})
    n = calendar_sync.sync()
    assert n["deleted_there"] == 1 and n["deleted_here"] == 1
    assert a not in google.events
    titles = {e["title"]: e for e in _events()}
    assert set(titles) == {"Offsite"} and titles["Offsite"]["start"] == "2027-06-01T09:00"
    assert ce.load()["events"] == list(titles.values())  # the tombstone is gone too


def test_both_sides_changed_the_newer_wins(google):
    gid = google.add("Team call", datetime(2026, 10, 21, 14, 0), datetime(2026, 10, 21, 15, 0))
    calendar_sync.sync()
    ce.calendar("move", event="team call", when="wednesday 4pm")
    state = ce.load()
    state["events"][0]["updated"] -= 3600  # the local move is an hour old
    ce.save(state)
    google.touch(gid, summary="Team call v2")  # Google's change is now
    calendar_sync.sync()
    assert "Wed 21 Oct 14:00-15:00 Team call v2" in ce.calendar("agenda")
    assert google.events[gid]["summary"] == "Team call v2"


def test_edited_here_after_google_deleted_it_comes_back(google):
    gid = google.add("Team call", datetime(2026, 10, 21, 14, 0), datetime(2026, 10, 21, 15, 0))
    calendar_sync.sync()
    del google.events[gid]
    ce.calendar("move", event="team call", when="wednesday 4pm")
    assert calendar_sync.sync()["created_there"] == 1
    assert [g["summary"] for g in google.events.values()] == ["Team call"]


def test_cancelled_and_declined_events_are_left_out(google):
    google.add("Cancelled", datetime(2026, 10, 21, 9, 0), datetime(2026, 10, 21, 10, 0), status="cancelled")
    google.add("Declined", datetime(2026, 10, 21, 11, 0), datetime(2026, 10, 21, 12, 0),
               attendees=[{"email": "me@x", "self": True, "responseStatus": "declined"}])
    google.add("Kept", datetime(2026, 10, 21, 13, 0), datetime(2026, 10, 21, 14, 0))
    calendar_sync.sync()
    assert [e["title"] for e in _events()] == ["Kept"]


def test_a_failed_write_keeps_the_event_for_next_time(google, monkeypatch):
    ce.calendar("add", title="Dentist", when="tomorrow 3pm")
    real = google.handler

    def flaky(req):
        if req.method == "POST":
            return httpx.Response(503, json={})
        return real(req)
    monkeypatch.setattr(auth, "_http", lambda: httpx.Client(transport=httpx.MockTransport(flaky)))
    assert calendar_sync.sync()["failed"] == 1
    assert _events()[0]["dirty"] and not _events()[0]["gid"]
    monkeypatch.setattr(auth, "_http", lambda: httpx.Client(transport=httpx.MockTransport(real)))
    assert calendar_sync.sync()["created_there"] == 1 and len(google.events) == 1


# --- "Dentist in 10 minutes" ---

def test_nudge_announces_an_event_once_and_again_when_moved(cal, monkeypatch):
    from app.awareness import calendar_nudge
    import app.routines.scheduler as sched

    monkeypatch.setattr(calendar_nudge, "_announced", {})
    monkeypatch.setattr(sched, "_toast_unless_jarvis_in_front", lambda *a: None)
    emitted = []
    monitor = type("M", (), {"emit": lambda self, o: emitted.append(o) or o})()
    ce.calendar("add", title="Dentist", when="today 10:08")
    ce.calendar("add", title="Lunch", when="today 1pm")
    ce.calendar("add", title="Holiday", when="today")  # all day: never nudged
    assert [o.detail for o in calendar_nudge.nudge_once(monitor, NOW)] == ["Dentist in 8 minutes."]
    assert calendar_nudge.nudge_once(monitor, NOW + timedelta(minutes=1)) == []  # once
    ce.calendar("move", event="dentist", when="today 10:15")
    assert [o.detail for o in calendar_nudge.nudge_once(monitor, NOW + timedelta(minutes=6))] == ["Dentist in 9 minutes."]
    assert emitted[0].kind.startswith("calendar:") and emitted[0].spoken == "Dentist in 8 minutes."


def test_find_with_only_a_day_is_that_days_agenda(cal):
    ce.calendar("add", title="Dentist", when="tomorrow 3pm")
    for args in ({"when": "tomorrow"}, {"event": "tomorrow"}):
        assert ce.calendar("find", **args).startswith("Tue 20 Oct:\n- ")
    assert "1 found" in ce.calendar("find", event="dentist", when="tomorrow")


def test_cue_points_at_the_calendar_when_an_event_is_asked_for():
    from app.agent.tools.notes import turn_directive

    for msg in ("put the dentist on my calendar for tuesday at 3pm", "Add lunch with Sam to my calendar friday",
                "can you schedule a call with Raj in my calendar tomorrow 10am"):
        assert turn_directive(msg).startswith("CALENDAR:"), msg
    assert not turn_directive("what's on my calendar tomorrow?").startswith("CALENDAR:")
    assert turn_directive("add milk to my list").startswith("TASKS:")


def test_agenda_reads_longer_spans_and_names_the_next_event(cal):
    ce.calendar("add", title="Girija Anni birthday", when="2026-11-11 06:00")
    week = ce.calendar("agenda", when="this week")
    assert week.startswith("Nothing on the calendar for Mon 19 Oct - Sun 25 Oct.")
    assert "The next event is" in week and "Wed 11 Nov 06:00-07:00 Girija Anni birthday" in week
    for when in ("this year", "for the whole year", "next 2 months", "everything"):
        assert "Girija Anni birthday" in ce.calendar("agenda", when=when), when
    assert ce.calendar("agenda", when="this year").startswith("Mon 19 Oct 2026 - Mon 18 Oct 2027:")
    assert ce.calendar("agenda", when="next 3 days") == "Nothing on the calendar for Mon 19 Oct - Wed 21 Oct. " \
        "The next event is " + ce.label(_events()[0]) + "."


# --- all-day dates by name and yearly birthdays (2026-10-22) ---

def test_month_name_dates_are_all_day_this_year():
    assert ce.parse_when("sept 19", NOW) == (datetime(2026, 9, 19), True, "")
    assert ce.parse_when("19 dec", NOW) == (datetime(2026, 12, 19), True, "")
    assert ce.parse_when("feb 30", NOW)[2].startswith("Error")


def test_yearly_birthday_is_all_day_and_comes_back_next_year(cal):
    out = ce.calendar("add", title="Ravi's birthday", when="sept 19", repeat="yearly")
    assert out.startswith("Added e") and "Sat 19 Sep, all day Ravi's birthday (every year)" in out
    assert "Ravi's birthday (every year)" in ce.calendar("agenda", when="2027-09-19")
    assert "Nothing on the calendar" in ce.calendar("agenda", when="2027-09-20")


def test_repeat_must_be_yearly_weekly_or_empty(cal):
    for bad in ("monthly", "every blue moon", "until 2026-12-20", "weekly until someday"):
        assert ce.calendar("add", title="x", when="tomorrow 9am", repeat=bad).startswith("Error"), bad
    assert ce.calendar("add", title="x", when="2026-11-02 9am", repeat="weekly until 2026-10-01").startswith("Error: the repeat ends")
    assert _events() == []


def test_yearly_series_is_pushed_as_a_recurrence_and_its_copies_are_not_imported(google):
    ce.calendar("add", title="Ravi's birthday", when="2026-10-26", repeat="yearly")
    calendar_sync.sync()
    master = next(g for g in google.events.values() if g["summary"] == "Ravi's birthday")
    assert master["recurrence"] == ["RRULE:FREQ=YEARLY"]
    google.add("Ravi's birthday", "2026-10-26", "2026-10-27", recurringEventId=master["id"])
    assert calendar_sync.sync()["created_here"] == 0
    assert len(_events()) == 1



# --- weekly classes (a timetable, PLAN 4.23b) ---

@pytest.mark.parametrize("text,kind,days,until", [
    ("weekly", "weekly", [], ""),
    ("every week", "weekly", [], ""),
    ("weekly mon wed fri", "weekly", [0, 2, 4], ""),
    ("every Monday and Wednesday", "weekly", [0, 2], ""),
    ("tues, thurs", "weekly", [1, 3], ""),
    ("weekdays", "weekly", [0, 1, 2, 3, 4], ""),
    ("daily until dec 20", "weekly", [0, 1, 2, 3, 4, 5, 6], "2026-12-20"),
    ("weekly mon wed until 2026-12-20", "weekly", [0, 2], "2026-12-20"),
    ("weekly mon to fri until 2026-12-20", "weekly", [0, 1, 2, 3, 4], "2026-12-20"),
    ("every fri-mon", "weekly", [0, 4, 5, 6], ""),
    ("yearly", "yearly", [], ""),
    ("", "", [], ""),
])
def test_parse_repeat(text, kind, days, until):
    assert ce.parse_repeat(text, NOW) == (kind, days, until, "")


def test_a_weekly_class_shows_on_its_days_until_its_end(cal):
    out = ce.calendar("add", title="DBMS lecture", when="2026-10-19 10:00", duration="1h", repeat="weekly mon wed until 2026-10-28")
    assert out.startswith("Added e") and "DBMS lecture (every Mon, Wed until 28 Oct 2026)" in out
    week = ce.calendar("agenda", when="next 14 days")
    assert [re.search(r"\w{3} \d\d \w{3} [\d:-]+", l).group(0) for l in week.splitlines()[1:]] == [
        "Mon 19 Oct 10:00-11:00", "Wed 21 Oct 10:00-11:00", "Mon 26 Oct 10:00-11:00", "Wed 28 Oct 10:00-11:00"]
    assert "Nothing on the calendar" in ce.calendar("agenda", when="2026-11-02")      # past its end
    assert "Nothing on the calendar" in ce.calendar("agenda", when="2026-10-20")      # a Tuesday
    assert len(_events()) == 1                                                         # stored once, as a series


def test_a_class_starts_on_its_first_listed_day(cal):
    out = ce.calendar("add", title="OS lab", when="2026-10-19 14:00", repeat="weekly tue thu")  # given a Monday
    assert "Tue 20 Oct 14:00-15:00 OS lab (every Tue, Thu)" in out
    assert "(That time has already passed.)" not in ce.calendar("add", title="Old class", when="2026-10-05 9am", repeat="weekly")


def test_find_and_match_use_the_next_class_not_the_first(cal):
    ce.calendar("add", title="Networks", when="2026-09-07 11:00", repeat="weekly mon")
    found = ce.calendar("find", event="networks")
    assert "Mon 19 Oct 11:00-12:00 Networks (every Mon)" in found and "(past)" not in found  # 10:00 now: 11:00 today is next
    assert ce.calendar("delete", event="networks").startswith("Deleted:")


def test_moving_a_one_day_class_to_another_day_moves_that_day(cal):
    ce.calendar("add", title="Maths", when="2026-10-19 09:00", repeat="weekly mon")
    out = ce.calendar("move", event="maths", when="2026-10-20 09:00")    # Monday's class to Tuesday
    assert "(every Tue)" in out


def test_a_new_time_for_a_weekly_class_keeps_its_days(cal, monkeypatch):
    ce.calendar("add", title="DBMS lecture", when="2026-10-19 10:00", repeat="weekly mon wed until 2026-12-20")
    monkeypatch.setattr(ce, "_now", lambda: datetime(2026, 10, 22, 12, 0))      # a Thursday: '11am' alone lands on Friday
    out = ce.calendar("move", event="dbms", when="11am")
    assert "Mon 19 Oct 11:00-12:00 DBMS lecture (every Mon, Wed until 20 Dec 2026)" in out
    out = ce.calendar("move", event="dbms", when="wednesday 9am")              # a class day named: still a time change
    assert "Mon 19 Oct 09:00-10:00 DBMS lecture (every Mon, Wed until 20 Dec 2026)" in out
    out = ce.calendar("move", event="dbms", when="friday 9am")                 # which of Mon/Wed moves? ask
    assert out.startswith("Error: 'DBMS lecture' repeats on Mon, Wed")
    assert ce.load()["events"][0]["days"] == [0, 2]


def test_the_nudge_announces_each_weeks_class(cal, monkeypatch):
    from app.awareness import calendar_nudge
    import app.routines.scheduler as sched

    monkeypatch.setattr(calendar_nudge, "_announced", {})
    monkeypatch.setattr(sched, "_toast_unless_jarvis_in_front", lambda *a: None)
    monitor = type("M", (), {"emit": lambda self, o: o})()
    ce.calendar("add", title="DBMS lecture", when="2026-10-19 10:05", repeat="weekly mon")
    assert [o.detail for o in calendar_nudge.nudge_once(monitor, NOW)] == ["DBMS lecture in 5 minutes."]
    assert [o.detail for o in calendar_nudge.nudge_once(monitor, NOW + timedelta(days=7))] == ["DBMS lecture in 5 minutes."]


def test_weekly_series_goes_to_google_with_byday_until_and_its_time_zone(google):
    ce.calendar("add", title="DBMS lecture", when="2026-10-19 10:00", repeat="weekly mon wed until 2026-12-20")
    ce.calendar("add", title="Holiday camp", when="2026-10-24", repeat="weekly sat until 2026-11-14")   # all day
    n = calendar_sync.sync()
    assert n["created_there"] == 2 and n["failed"] == 0
    lecture = next(g for g in google.events.values() if g["summary"] == "DBMS lecture")
    rule = lecture["recurrence"][0]
    assert rule.startswith("RRULE:FREQ=WEEKLY;BYDAY=MO,WE;UNTIL=20261220T") and rule.endswith("Z")
    until_local = datetime.strptime(rule.split("UNTIL=")[1], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).astimezone()
    assert until_local.strftime("%Y-%m-%d %H:%M") == "2026-12-20 23:59"
    assert lecture["start"]["timeZone"] == lecture["end"]["timeZone"] == "Asia/Kolkata"
    camp = next(g for g in google.events.values() if g["summary"] == "Holiday camp")
    assert camp["recurrence"] == ["RRULE:FREQ=WEEKLY;BYDAY=SA;UNTIL=20261114"] and "timeZone" not in camp["start"]
    google.add("DBMS lecture", datetime(2026, 10, 21, 10), datetime(2026, 10, 21, 11), recurringEventId=lecture["id"])
    assert calendar_sync.sync()["created_here"] == 0          # Google's copies of Jarvis's own series aren't imported
    assert len(_events()) == 2


def test_a_timed_yearly_series_also_carries_the_time_zone(google):
    ce.calendar("add", title="Team anniversary", when="2026-10-26 09:00", repeat="yearly")
    assert calendar_sync.sync()["failed"] == 0
    g = next(g for g in google.events.values() if g["summary"] == "Team anniversary")
    assert g["start"]["timeZone"] == "Asia/Kolkata"



def test_the_approval_card_says_a_whole_series_goes(cal):
    ce.calendar("add", title="DBMS lecture", when="2026-10-19 10:00", repeat="weekly mon wed until 2026-12-20")
    ce.calendar("add", title="Dentist", when="tomorrow 3pm")
    d = evaluate_tool_permission("calendar", {"action": "delete", "event": "dbms"})
    assert not d.allowed and d.reason == ('Deletes every "DBMS lecture" (Mon, Wed until 20 Dec 2026, the whole series) '
                                          "from your calendar (and Google Calendar).")
    assert "the whole series" in evaluate_tool_permission("calendar", {"action": "move", "event": "dbms", "when": "11am"}).reason
    assert evaluate_tool_permission("calendar", {"action": "delete", "event": "dentist"}).reason == (
        'Deletes "dentist" from your calendar (and Google Calendar).')
