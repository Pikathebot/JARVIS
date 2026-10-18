"""The stream loop always ends with an answer: rounds spent or a repeated call -> one answer round."""
from unittest.mock import MagicMock

import pytest

from app.agent import orchestrator as orch_mod
from app.agent.permissions import ChatMode
from app.agent.tools import registry
from test_orchestrator_tool_turns import ScriptedProvider, _text_turn
from test_taint import _call


def _orch():
    o = orch_mod.AgentOrchestrator()
    o.memory_store = MagicMock()
    return o


async def _run(provider, tmp_path, max_iterations=5):
    o = _orch()
    return [ev async for ev in o._run_provider_stream_loop(
        provider=provider, session_id="loop-guard", model="main", system_prompt="sys", tools=[registry.web_search],
        conversation_messages=[{"role": "user", "content": "who made wukong?"}], chat_mode=ChatMode.WORKSPACE,
        workspace_path=tmp_path, max_iterations=max_iterations)]


@pytest.fixture(autouse=True)
def _fake_search(monkeypatch):
    monkeypatch.setitem(registry.TOOL_FUNCTIONS, "web_search", lambda query, **_: f"results for {query}")


@pytest.mark.asyncio
async def test_spent_rounds_end_with_an_answer_not_silence(tmp_path):
    turns = [_call(f"c{i}", "web_search", {"query": f"wukong studio {i}"}) for i in range(3)]
    p = ScriptedProvider(turns + [_text_turn("Game Science made it.")])
    events = await _run(p, tmp_path, max_iterations=3)
    assert events[-1]["event"] == "done" and events[-1]["data"]["response"] == "Game Science made it."
    assert p.calls[-1]["messages"][-1]["content"] == orch_mod._NUDGE_FINAL
    assert len([e for e in events if e["event"] == "tool_end"]) == 3
    assert "rounds-spent" in events[-1]["data"]["metrics"]["nets"]


@pytest.mark.asyncio
async def test_a_repeated_call_is_not_run_again_and_the_model_answers(tmp_path):
    same = {"query": "wukong studio"}
    p = ScriptedProvider([_call("c1", "web_search", same), _call("c2", "web_search", same),
                          _text_turn("It was Game Science.")])
    events = await _run(p, tmp_path)
    assert len([e for e in events if e["event"] == "tool_end"]) == 1
    assert p.calls[-1]["messages"][-1]["content"] == orch_mod._NUDGE_REPEAT
    assert events[-1]["data"]["response"] == "It was Game Science."
    assert "duplicate action loop" not in "".join(e["data"].get("delta", "") for e in events if e["event"] == "token")


@pytest.mark.asyncio
async def test_a_tool_call_in_the_answer_round_falls_back_to_plain_prose(tmp_path):
    p = ScriptedProvider([_call("c1", "web_search", {"query": "a"}), _call("c2", "web_search", {"query": "b"})])
    events = await _run(p, tmp_path, max_iterations=1)
    assert events[-1]["event"] == "done" and events[-1]["data"]["response"] == "prose fallback"
    assert p.calls[-1].get("chat")  # the plain completion
