"""
An approval resumes the paused turn instead of re-running it (PLAN 4.13, second device test #1): a
write whose content the model wrote ran into a new approval card on every re-run, forever.
"""
import time
from unittest.mock import MagicMock

import pytest

from app.agent import paused_turns
from app.agent.permissions import ChatMode
from app.agent.tools import registry
from app.agent.tools.registry import fetch_url, remember, write_file
from test_orchestrator_tool_turns import ScriptedProvider, _orchestrator, _text_turn
from test_taint import _call

URL = "https://en.wikipedia.org/wiki/Kokoro"


@pytest.fixture(autouse=True)
def _page(monkeypatch):
    monkeypatch.setitem(registry.TOOL_FUNCTIONS, "fetch_url", lambda url, **_: "Kokoro is a novel by Soseki.")


async def _pause(orch, tmp_path, session_id, summary):
    """Turn 1: read the page, then a write with model-written content -> the approval card."""
    p1 = ScriptedProvider([_call("f1", "fetch_url", {"url": URL}),
                           _call("w1", "write_file", {"file_path": "summary.md", "content": summary})])
    events = [ev async for ev in orch._run_provider_stream_loop(
        provider=p1, session_id=session_id, model="main", system_prompt="sys",
        conversation_messages=[{"role": "user", "content": f"summarise {URL} and save it"}],
        tools=[fetch_url, write_file, remember], chat_mode=ChatMode.WORKSPACE, workspace_path=tmp_path,
        tool_context={"workspace_path": str(tmp_path), "session_id": session_id,
                      "user_message": f"summarise {URL} and save it"})]
    assert events[-1]["event"] == "confirmation_required"
    return [p["action_id"] for p in events[-1]["data"]["pending_confirmations"]]


async def _approve(orch, session_id, ids):
    return [ev async for ev in orch.run_stream(f"summarise {URL} and save it", session_id=session_id,
                                               approved_action_ids=ids)]


@pytest.mark.asyncio
async def test_an_approval_runs_the_parked_write_once_and_carries_on(tmp_path):
    orch = _orchestrator()
    orch.memory_store = MagicMock()
    ids = await _pause(orch, tmp_path, "pause-1", "Kokoro: Soseki's 1914 novel.")
    assert not (tmp_path / "summary.md").exists()

    # A re-run would write a different summary (and ask again); the resume writes the parked one.
    orch.provider = p2 = ScriptedProvider([_text_turn("Saved it to summary.md.")])
    events = await _approve(orch, "pause-1", ids)

    assert (tmp_path / "summary.md").read_text() == "Kokoro: Soseki's 1914 novel."
    assert [e["data"]["tool"] for e in events if e["event"] == "tool_end"] == ["write_file"]
    assert events[-1]["event"] == "done" and events[-1]["data"]["response"] == "Saved it to summary.md."
    assert [t["tool"] for t in events[-1]["data"]["tools_used"]] == ["fetch_url", "write_file"]
    # one model round after the write, picking up from the conversation so far
    assert len(p2.calls) == 1
    roles = [m["role"] for m in p2.calls[0]["messages"]]
    assert roles == ["system", "user", "assistant", "tool", "assistant", "tool"]
    # the user's message was stored by the first request, not again
    assert not [c for c in orch.memory_store.append_message.call_args_list if c.kwargs.get("role") == "user"]
    assert paused_turns.take("pause-1", ids) is None  # consumed


@pytest.mark.asyncio
async def test_the_resumed_turn_is_still_tainted(tmp_path):
    orch = _orchestrator()
    orch.memory_store = MagicMock()
    ids = await _pause(orch, tmp_path, "pause-2", "a summary")
    orch.provider = ScriptedProvider([_call("m1", "remember", {"content": "the user likes Soseki"})])
    events = await _approve(orch, "pause-2", ids)
    assert events[-1]["event"] == "confirmation_required"
    pending = events[-1]["data"]["pending_confirmations"]
    assert [p["tool"] for p in pending] == ["remember"] and "read a web page" in pending[0]["reason"]
    # and that pause is parked in turn
    paused_turns.forget("pause-2")


@pytest.mark.asyncio
async def test_a_denied_or_expired_park_is_not_resumed(tmp_path):
    orch = _orchestrator()
    orch.memory_store = MagicMock()
    ids = await _pause(orch, tmp_path, "pause-3", "a summary")
    paused_turns.drop_action(ids[0])
    assert paused_turns.take("pause-3", ids) is None

    ids = await _pause(orch, tmp_path, "pause-4", "a summary")
    assert paused_turns.take("pause-4", ["act_unrelated"]) is None  # another card: left parked
    paused_turns._parked["pause-4"].expires_at = time.monotonic() - 1
    assert paused_turns.take("pause-4", ids) is None and "pause-4" not in paused_turns._parked
