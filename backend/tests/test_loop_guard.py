"""The stream loop always ends with an answer: rounds spent or a repeated call -> one answer round."""
from unittest.mock import MagicMock

import pytest

from app.agent import orchestrator as orch_mod
from app.agent.permissions import ChatMode
from app.agent.tool_errors import NEXT_STEP
from app.agent.tools import registry
from app.agent.validator import CallHistory
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


# --- result hints (PLAN 4.15 #4) ---

def _tool_messages(provider):
    return [m["content"] for m in provider.calls[-1]["messages"] if m.get("role") == "tool"]


@pytest.mark.asyncio
async def test_a_failed_call_gets_its_next_step_and_a_second_alike_failure_the_stop_line(tmp_path, monkeypatch):
    monkeypatch.setitem(registry.TOOL_FUNCTIONS, "web_search",
                        lambda query, **_: f"Error: tool 'web_search' got no page for {query}: not found")
    p = ScriptedProvider([_call("c1", "web_search", {"query": "a"}), _call("c2", "web_search", {"query": "b"}),
                          _text_turn("I couldn't find it.")])
    events = await _run(p, tmp_path)
    first, second = _tool_messages(p)
    assert first.endswith(NEXT_STEP["not_found"])
    assert "failed the same way twice this turn" in second
    # the card shows the tool's own text, not the hint
    cards = [e["data"]["result"] for e in events if e["event"] == "tool_end"]
    assert all("Next:" not in c and "twice" not in c for c in cards)
    assert events[-1]["data"]["metrics"]["nets"].count("result-hint") == 2


@pytest.mark.asyncio
async def test_the_third_web_lookup_asks_for_an_answer_but_still_runs(tmp_path):
    turns = [_call(f"c{i}", "web_search", {"query": f"q{i}"}) for i in range(3)]
    p = ScriptedProvider(turns + [_text_turn("Here is what I found.")])
    await _run(p, tmp_path)
    results = _tool_messages(p)
    assert [r.startswith("results for q") for r in results] == [True, True, True]  # nothing refused
    assert "web lookups this turn" not in results[1]
    assert "3 web lookups this turn" in results[2]


@pytest.mark.asyncio
async def test_a_successful_call_gets_no_hint(tmp_path):
    p = ScriptedProvider([_call("c1", "web_search", {"query": "a"}), _text_turn("Done.")])
    await _run(p, tmp_path)
    assert _tool_messages(p) == ["results for a"]


def test_counts_survive_a_parked_turn():
    """CallHistory is what paused_turns parks and restores, so a resumed turn keeps counting."""
    h = CallHistory(window=3)
    h.hint_for("web_search", "r1")
    h.hint_for("research", "r2")
    assert "3 web lookups" in h.hint_for("web_search", "r3")
    assert h.hint_for("read_file", "Error: 'x' does not exist.") == NEXT_STEP["not_found"]
    assert "twice" in h.hint_for("read_file", "Error: 'y' does not exist.")
    assert h.hint_for("read_file", "x", invalid_args=True) == NEXT_STEP["bad_input"]

