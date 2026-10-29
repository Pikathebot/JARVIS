"""The training log (PLAN §4 step 1): nets that fired and "got it wrong" flags, for the fine-tune."""
import json
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.agent import training_log
from app.agent.orchestrator import AgentOrchestrator
from app.agent.permissions import ChatMode
from app.agent.tools.registry import delete_file, read_file
from app.main import app
from tests.test_orchestrator_tool_turns import ScriptedProvider, _text_turn


@pytest.fixture
def log_path(tmp_path, monkeypatch):
    path = tmp_path / "training_log.jsonl"
    monkeypatch.setattr(training_log, "LOG_PATH", path)
    return path


def _records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def _turn(**overrides):
    args = dict(session_id="s1", model="main", user_message="add milk to my list", reply="Added milk.",
                tools_used=[{"tool": "tasks", "args": {"action": "add", "title": "milk"}, "status": "success",
                             "result": "x" * 5000}],
                nets=["claim-checked"], sent_back=[{"net": "claim-checked", "draft": "Done.", "note": "Nothing ran."}],
                ephemeral=False)
    args.update(overrides)
    return training_log.close_turn(**args)


def test_a_turn_where_a_net_fired_is_logged_with_its_draft_and_note(log_path):
    turn_id = _turn()
    (rec,) = _records(log_path)
    assert rec["kind"] == "net" and rec["turn_id"] == turn_id
    assert rec["user_message"] == "add milk to my list" and rec["reply"] == "Added milk."
    assert rec["sent_back"] == [{"net": "claim-checked", "draft": "Done.", "note": "Nothing ran."}]
    assert len(rec["tools"][0]["result"]) < 1100  # tool output is clipped


def test_a_clean_turn_is_kept_for_a_flag_but_not_logged(log_path):
    assert _turn(nets=["fit-window"], sent_back=[])  # fitting the window is the harness, not the model
    assert _records(log_path) == []


def test_off_the_record_turns_never_reach_the_log(log_path):
    assert _turn(ephemeral=True) is None
    training_log.record_capture("memory", session_id="s1", model="main", user_message="x", saved="Saved.",
                                ephemeral=True)
    assert _records(log_path) == []


def test_got_it_wrong_records_the_turn_and_what_was_meant(log_path):
    turn_id = _turn(nets=[], sent_back=[])
    assert not training_log.flag_wrong("nope")
    assert training_log.flag_wrong(turn_id)
    assert training_log.flag_wrong(turn_id, "  I meant the shopping list  ")
    assert training_log.unflag(turn_id)
    first, second, withdrawn = _records(log_path)
    assert first["kind"] == second["kind"] == "wrong" and first["meant"] == ""
    assert second["meant"] == "I meant the shopping list" and second["reply"] == "Added milk."
    assert withdrawn == {"kind": "unflagged", "time": withdrawn["time"], "turn_id": turn_id}


def test_the_feedback_endpoint(log_path):
    turn_id = _turn(nets=[], sent_back=[])
    client = TestClient(app)
    assert client.post("/api/feedback/unknown/wrong", json={}).status_code == 404
    assert client.post(f"/api/feedback/{turn_id}/wrong", json={"meant": "the other file"}).json() == {"saved": True}
    assert client.delete(f"/api/feedback/{turn_id}/wrong").status_code == 200
    assert [r["kind"] for r in _records(log_path)] == ["wrong", "unflagged"]


@pytest.mark.asyncio
async def test_a_sent_back_claim_reaches_the_log_and_done_carries_the_turn_id(tmp_path, log_path):
    provider = ScriptedProvider([
        _text_turn("I've deleted old.txt."),
        _text_turn("I haven't deleted it yet."),
    ])
    orch = AgentOrchestrator()
    orch.memory_store = MagicMock()
    events = [ev async for ev in orch._run_provider_stream_loop(
        provider=provider, session_id="log-test", model="main", system_prompt="sys",
        conversation_messages=[{"role": "user", "content": "delete old.txt"}],
        tools=[read_file, delete_file], chat_mode=ChatMode.WORKSPACE, workspace_path=tmp_path,
    )]
    done = events[-1]["data"]
    (rec,) = _records(log_path)
    assert done["turn_id"] == rec["turn_id"]
    assert rec["user_message"] == "delete old.txt" and rec["reply"] == "I haven't deleted it yet."
    assert rec["nets"] == ["claim-checked"]
    assert rec["sent_back"][0]["draft"] == "I've deleted old.txt."
    assert "delete_file" in rec["sent_back"][0]["note"]


@pytest.mark.asyncio
async def test_a_capture_that_saved_something_is_logged(log_path):
    orch = AgentOrchestrator()

    async def saved():
        return ["Error: nope", "Recorded under Decisions."]

    async def nothing():
        return None

    await orch._logged_capture("project", saved(), "main", "we picked sqlite", {"session_id": "s1"})
    await orch._logged_capture("memory", nothing(), "main", "hi", {"session_id": "s1"})
    (rec,) = _records(log_path)
    assert rec["kind"] == "capture" and rec["net"] == "project" and rec["saved"] == "Recorded under Decisions."
