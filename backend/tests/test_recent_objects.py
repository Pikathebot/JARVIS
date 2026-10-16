"""The per-session record of what tools really did (PLAN 4.13 #4)."""
from app.agent import recent_objects as ro
from app.agent.orchestrator import build_turn_context


def test_records_objects_and_outcomes_newest_first():
    sid = "ro-1"
    ro.forget(sid)
    ro.begin_turn(sid)
    ro.record(sid, {"tool": "write_file", "args": {"file_path": "secret.txt", "content": "hello"}, "status": "success"})
    ro.begin_turn(sid)
    ro.record(sid, {"tool": "web_search", "args": {"query": "bleach new arc"}, "status": "success"})
    ro.record(sid, {"tool": "kill_process", "args": {"pid_or_name": "discord"}, "status": "error"})
    ro.begin_turn(sid)
    text = ro.describe(sid)
    lines = text.splitlines()
    assert lines[0].startswith("RECENT ACTIONS")
    assert lines[1] == "- 1 turn ago: kill_process discord (failed)"
    assert lines[2] == '- 1 turn ago: web_search "bleach new arc" (ok)'
    assert lines[3] == "- 2 turns ago: write_file secret.txt (ok)"


def test_non_stream_items_infer_success_from_the_result():
    sid = "ro-2"
    ro.forget(sid)
    ro.begin_turn(sid)
    ro.record(sid, {"tool": "delete_file", "args": {"file_path": "drafts"}, "result": "Error: not found: drafts"})
    ro.record(sid, {"tool": "set_volume", "args": {"level": 100}, "result": "Volume set to 100%."})
    ro.begin_turn(sid)
    assert "delete_file drafts (failed)" in ro.describe(sid)
    assert "set_volume level 100 (ok)" in ro.describe(sid)


def test_old_entries_age_out_and_forget_clears():
    sid = "ro-3"
    ro.forget(sid)
    ro.begin_turn(sid)
    ro.record(sid, {"tool": "remind_me", "args": {"text": "stretch", "when": "in 2 minutes"}, "status": "success"})
    for _ in range(ro.MAX_TURNS_BACK):
        ro.begin_turn(sid)
    assert ro.describe(sid) == ""
    ro.record(sid, {"tool": "remember", "args": {"content": "prefers dark mode"}, "status": "success"})
    ro.forget(sid)
    assert ro.describe(sid) == ""


def test_the_block_rides_in_the_turn_context_only_when_there_is_one():
    assert "RECENT ACTIONS" not in build_turn_context("")
    block = "RECENT ACTIONS (from real tool results, newest first):\n- 1 turn ago: write_file a.txt (ok)"
    assert block in build_turn_context("", recent=block)
