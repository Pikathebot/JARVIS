"""PLAN 4.16 phase A: notes and tasks.md as plain Markdown in the user's notes folder."""
from datetime import datetime

import pytest

from app.agent import orchestrator as orch_mod
from app.agent.permissions import RiskTier, evaluate_tool_permission
from app.agent.tools import notes as notes_mod
from app.agent.tools.notes import notes, parse_due, tasks
from app.agent.tools.registry import execute_tool
from app.config import settings

NOW = datetime(2026, 10, 18, 15, 0)  # a Sunday


_SCHED: list = [None]


def _routines() -> dict:
    return _SCHED[0].routines


class FakeScheduler:
    def __init__(self):
        self.routines = {}

    def create(self, r):
        self.routines[r.id] = r
        return r

    def delete(self, rid):
        return self.routines.pop(rid, None) is not None


@pytest.fixture
def folder(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "notes_dir", str(tmp_path / "Jarvis Notes"))
    monkeypatch.setattr(notes_mod, "_now", lambda: NOW)
    sched = FakeScheduler()
    monkeypatch.setattr(notes_mod, "_scheduler", lambda: sched)
    monkeypatch.setattr(notes_mod, "_is_ephemeral", lambda sid: sid == "off-record")
    _SCHED[0] = sched
    return tmp_path / "Jarvis Notes"


# --- notes ---

def test_a_note_is_a_markdown_file_named_from_its_title(folder):
    assert notes("add", title="Wi-Fi: guest network?", text="Password is on the router.") == \
        "Saved the note 'Wi-Fi guest network'."
    assert (folder / "Wi-Fi guest network.md").read_text(encoding="utf-8") == "Password is on the router.\n"


def test_add_does_not_overwrite_and_append_adds(folder):
    notes("add", title="Ideas", text="A HUD widget for timers.")
    assert notes("add", title="ideas", text="x").startswith("Error: a note called 'Ideas' already exists")
    assert notes("append", title="ideas", text="Voice shortcuts.") == "Added to the note 'Ideas'."
    assert (folder / "Ideas.md").read_text(encoding="utf-8") == "A HUD widget for timers.\n\nVoice shortcuts.\n"


def test_find_read_and_list_see_notes_in_subfolders_but_not_obsidian_internals(folder):
    (folder / "Uni").mkdir(parents=True)
    (folder / "Uni" / "Physics.md").write_text("Exam on the 12th.\nBring a calculator.\n", encoding="utf-8")
    (folder / ".obsidian").mkdir()
    (folder / ".obsidian" / "workspace.md").write_text("exam", encoding="utf-8")
    (folder / "tasks.md").write_text("- [ ] exam prep\n", encoding="utf-8")
    found = notes("find", query="exam")
    assert "Uni/Physics" in found and "Exam on the 12th." in found and "obsidian" not in found and "tasks" not in found
    assert notes("read", title="physics").startswith("Note 'Uni/Physics':\nExam on the 12th.")
    assert notes("list").startswith("1 notes")
    assert "no note called" in notes("read", title="chemistry")
    # a match on the title alone still shows the note's text, so one call answers
    assert "Exam on the 12th." in notes("find", query="physics")


def test_off_the_record_chats_can_read_but_not_write(folder):
    notes("add", title="Ideas", text="one")
    assert notes("add", title="Secret", text="x", session_id="off-record").startswith("Not saved")
    assert notes("append", title="Ideas", text="x", session_id="off-record").startswith("Not saved")
    assert "Ideas" in notes("list", session_id="off-record")
    assert tasks("add", text="x", session_id="off-record").startswith("Not saved")
    assert not (folder / "Secret.md").exists() and not (folder / "tasks.md").exists()


# --- tasks ---

@pytest.mark.parametrize("due,expected", [
    ("tomorrow", ("2026-10-19", "")), ("friday", ("2026-10-23", "")), ("on Sunday", ("2026-10-25", "")),
    ("2026-10-20", ("2026-10-20", "")), ("5pm", ("2026-10-18", "17:00")), ("tomorrow 9am", ("2026-10-19", "09:00")),
    ("in 2 hours", ("2026-10-18", "17:00")), ("", ("", "")),
])
def test_due_times(due, expected):
    assert parse_due(due, NOW)[:2] == expected


def test_add_list_done_round_trip_with_a_reminder(folder):
    out = tasks("add", text="Pay rent", due="tomorrow 5pm")
    assert out.startswith("Added t") and "(due 2026-10-19 17:00)" in out and "remind you" in out
    rid, routine = next(iter(_routines().items()))
    assert routine.at == "2026-10-19T17:00" and routine.message == "Due now: Pay rent" and routine.reminder
    tasks("add", text="Buy milk")
    text = (folder / "tasks.md").read_text(encoding="utf-8")
    assert text.startswith("# Tasks\n") and f"- [ ] Pay rent (due 2026-10-19 17:00) <!-- id:" in text and f"rem:{rid}" in text
    listed = tasks("list")
    assert listed.startswith("2 open:") and listed.index("Pay rent") < listed.index("Buy milk")  # dated first
    assert tasks("done", task_id="rent") == "Ticked off: Pay rent."
    assert _routines() == {}  # its reminder went with it
    assert "- [x] Pay rent (due 2026-10-19 17:00) (done 2026-10-" in (folder / "tasks.md").read_text(encoding="utf-8")
    assert tasks("list").startswith("1 open:\n- ") and "Buy milk" in tasks("list")
    assert "(1 done.)" in tasks("list")


def test_a_hand_typed_line_is_understood_and_kept_with_the_rest_of_the_file(folder):
    folder.mkdir(parents=True)
    (folder / "tasks.md").write_text("# My list\n\nSome words I wrote.\n- [ ] Call mum\n* [x] Old thing\n",
                                     encoding="utf-8")
    listed = tasks("list")
    assert "Call mum" in listed and "Old thing" not in listed and "(1 done.)" in listed
    assert (folder / "tasks.md").read_text(encoding="utf-8").endswith("- [ ] Call mum\n* [x] Old thing\n")  # reading wrote nothing
    tasks("done", task_id="call mum")
    text = (folder / "tasks.md").read_text(encoding="utf-8")
    assert text.startswith("# My list\n\nSome words I wrote.\n- [x] Call mum (done ")
    assert "<!-- id:t" in text


def test_matching_is_by_id_or_words_and_ambiguity_is_an_error(folder):
    tasks("add", text="Buy milk")
    tasks("add", text="Buy bread")
    assert tasks("done", task_id="buy").startswith("Error: several tasks match")
    tid = tasks("add", text="Email Sam").split()[1].rstrip(":")
    assert tasks("done", task_id=tid) == "Ticked off: Email Sam."
    assert tasks("done", task_id="dentist").startswith("Error: no task matches")
    assert tasks("add", text="buy milk").startswith("Already on the list")


def test_edit_reopen_and_remove(folder):
    tasks("add", text="Dentist", due="friday")
    assert tasks("edit", task_id="dentist", due="tomorrow 10am").startswith("Updated: ")
    assert len(_routines()) == 1
    assert "Dentist appointment" in tasks("edit", task_id="dentist", text="Dentist appointment")
    assert len(_routines()) == 1  # rescheduled, not doubled
    tasks("done", task_id="dentist")
    assert tasks("reopen", task_id="dentist").startswith("Back on the list") and len(_routines()) == 1
    assert tasks("remove", task_id="dentist") == "Removed: Dentist appointment."
    assert _routines() == {} and "Dentist" not in (folder / "tasks.md").read_text(encoding="utf-8")


def test_a_due_time_already_past_adds_the_task_without_a_reminder(folder):
    assert tasks("add", text="Stretch", due="2026-10-18 09:00").endswith("already passed, so no reminder.")
    assert _routines() == {}


# --- wiring ---

def test_both_tools_are_offered_in_freeform_with_schemas():
    from app.agent.tools.registry import TOOL_SCHEMAS, tools_for_space
    names = [t.__name__ for t in tools_for_space("FREEFORM")]
    assert "notes" in names and "tasks" in names and {"notes", "tasks"} <= set(TOOL_SCHEMAS)


def test_execute_tool_passes_the_session(folder):
    assert execute_tool("tasks", {"action": "add", "text": "x"}, {"session_id": "off-record", "chat_mode": "FREEFORM"}) \
        .startswith("Not saved")


def test_permissions_removing_asks_the_rest_runs():
    assert evaluate_tool_permission("tasks", {"action": "add", "text": "milk"}).allowed
    assert evaluate_tool_permission("notes", {"action": "add", "title": "x", "text": "y"}).allowed
    remove = evaluate_tool_permission("tasks", {"action": "remove", "task_id": "milk"})
    assert not remove.allowed and remove.risk_tier == RiskTier.CONFIRMATION_REQUIRED and "milk" in remove.reason


def test_after_reading_a_web_page_writing_asks_but_reading_does_not():
    assert not evaluate_tool_permission("notes", {"action": "add", "title": "x", "text": "y"}, tainted_by="a web page").allowed
    assert not evaluate_tool_permission("tasks", {"action": "add", "text": "y"}, tainted_by="a web page").allowed
    assert evaluate_tool_permission("notes", {"action": "find", "query": "y"}, tainted_by="a web page").allowed
    assert evaluate_tool_permission("tasks", {"action": "list"}, tainted_by="a web page").allowed


def test_reading_notes_is_not_a_change_for_the_claim_check():
    assert not orch_mod.changed_something([{"tool": "notes", "args": {"action": "find"}, "status": "success"}])
    assert not orch_mod.changed_something([{"tool": "tasks", "args": {"action": "list"}, "status": "success"}])
    assert orch_mod.changed_something([{"tool": "tasks", "args": {"action": "add"}, "status": "success"}])


# --- the per-turn cue ---

def test_cue_points_at_notes_when_a_note_is_asked_for(folder):
    for msg in ("note that the guest wifi password is on the router", "Make a note: dentist Tuesday",
                "jot this down -- call the bank", "add this to my notes"):
        assert notes_mod.turn_directive(msg).startswith("NOTES:"), msg
    for msg in ("add milk to my list", "put call mum on the to-do list", "Add eggs and bread to my tasks"):
        assert notes_mod.turn_directive(msg).startswith("TASKS: the user asked to add"), msg
    for msg in ("what did I note about the dentist?", "notes are useful", "write me a poem",
                "add salt to the soup", "what's on my list?"):
        assert notes_mod.turn_directive(msg) == "", msg


def test_cue_names_the_one_open_task_that_sounds_done(folder):
    tasks("add", text="Buy milk")
    tasks("add", text="Call mum")
    tid = notes_mod._parse_tasks(notes_mod._read_tasks()[1])[0].id
    cue = notes_mod.turn_directive("I bought the milk")
    assert cue.startswith("TASKS:") and "Buy milk" in cue and tid in cue
    assert notes_mod.turn_directive("I bought a new phone") == ""      # no open task shares a word
    assert notes_mod.turn_directive("did I buy milk?") == ""          # not a "done" statement
    tasks("done", task_id=tid)
    assert notes_mod.turn_directive("I bought the milk") == ""        # already ticked off


def test_cue_stays_quiet_when_two_tasks_match(folder):
    tasks("add", text="Call mum")
    tasks("add", text="Call the bank")
    assert notes_mod.turn_directive("I called mum and the bank") == ""


def test_cue_without_a_tasks_file_is_empty(folder):
    assert notes_mod.turn_directive("I finished the report") == ""


async def _user_turn(monkeypatch, store, message):
    from unittest.mock import AsyncMock, MagicMock

    from tests.fake_providers import ClientBackedProvider

    sent = []

    async def reply(model=None, messages=None, tools=None):
        sent.append(messages)
        yield {"type": "token", "delta": "Done."}
        yield {"type": "done", "content": "Done.", "tool_calls": []}

    monkeypatch.setattr(orch_mod.AgentOrchestrator, "_slot_has_vision", lambda self, slot, provider: False)
    skills = MagicMock(match_skills=MagicMock(return_value=[]), build_skill_prompt_injection=MagicMock(return_value=""))
    orch = orch_mod.AgentOrchestrator(provider=ClientBackedProvider(MagicMock(chat_stream=reply)),
                                      openrouter_client=MagicMock(), memory_store=store,
                                      compactor=MagicMock(compact=AsyncMock(return_value=([], None))),
                                      skills_loader=skills,
                                      mcp_manager=MagicMock(get_tool_definitions=MagicMock(return_value=[])))
    _ = [ev async for ev in orch.run_stream(user_message=message, session_id="n1", chat_mode="FREEFORM",
                                             voice_route="act")]
    return [m for m in sent[0] if m["role"] == "user"][-1]["content"]


@pytest.mark.asyncio
async def test_the_cue_rides_on_the_user_turn_and_not_in_ephemeral_chats(folder, monkeypatch):
    from unittest.mock import MagicMock

    from app.memory.ephemeral import EphemeralMemoryStore

    store = MagicMock()
    store.get_or_create_session.return_value = {"chat_mode": "FREEFORM"}
    store.get_messages.return_value = []
    msg = "note that the guest wifi password is on the router"
    assert "NOTES: the user asked for a note" in await _user_turn(monkeypatch, store, msg)
    assert "NOTES:" not in await _user_turn(monkeypatch, EphemeralMemoryStore(store), msg)
