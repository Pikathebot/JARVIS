"""Thinking that goes in circles is stopped and asked again with thinking off (2026-10-24)."""
import pytest

from app.agent.orchestrator import _NUDGE_THINKING_LOOP
from app.agent.permissions import ChatMode
from app.agent.thinking_loop import LoopWatch, capped
from app.agent.tools.registry import read_file
from tests.test_orchestrator_tool_turns import ScriptedProvider, _orchestrator

LOOP = ("The user said 'search for a document project u'. Maybe they mean a project called U, or a "
        "document about project U. Let me think about which one. ")


def test_a_repeating_think_is_caught_and_a_varied_one_is_not():
    w = LoopWatch()
    assert not any(w.feed(f"Step {i}: weighing option {i * 7} against the {i}th constraint now. ") for i in range(200))
    w = LoopWatch()
    hits = [w.feed(LOOP) for _ in range(40)]
    assert any(hits) and hits.index(True) < 30


def test_capped_round():
    assert capped({"predicted_n": 8192}, 8192) and not capped({"predicted_n": 900}, 8192)
    assert not capped(None, 8192) and not capped({"predicted_n": 8192}, 0)


class StreamCounter(ScriptedProvider):
    def __init__(self, turns):
        super().__init__(turns)
        self.thinking = []

    async def stream_chat(self, messages, model=None, tools=None, temperature=None, profile="general", timeout=None, thinking=None):
        self.thinking.append(thinking)
        async for ev in super().stream_chat(messages, model, tools, temperature, profile, timeout, thinking):
            yield ev


@pytest.mark.asyncio
async def test_a_loop_is_stopped_and_retried_without_thinking(tmp_path):
    looping = [{"event": "reasoning_delta", "content": LOOP} for _ in range(200)]
    looping.append({"event": "done", "raw": {"content": "", "reasoning": "", "tool_calls": None}})
    provider = StreamCounter([
        looping,
        [{"event": "text_delta", "content": "Which do you mean: a project called U, or a document about it?"},
         {"event": "done", "raw": {"content": "Which do you mean: a project called U, or a document about it?",
                                   "reasoning": "", "tool_calls": None}}],
    ])
    orch = _orchestrator()
    events = [ev async for ev in orch._run_provider_stream_loop(
        provider=provider, session_id="loop", model="main", system_prompt="sys",
        conversation_messages=[{"role": "user", "content": "search for a document project u"}],
        tools=[read_file], chat_mode=ChatMode.FREEFORM, workspace_path=tmp_path, thinking=True,
    )]
    done = events[-1]["data"]
    assert done["response"].startswith("Which do you mean")
    assert provider.thinking == [True, False]
    assert "thinking-loop" in done["metrics"]["nets"]
    second = provider.calls[1]["messages"]
    assert second[-1] == {"role": "user", "content": _NUDGE_THINKING_LOOP}
    assert not any(LOOP in (m.get("content") or "") for m in second)  # the loop isn't sent back
    reasoning_events = [e for e in events if e["event"] == "reasoning"]
    assert len(reasoning_events) < 60  # stopped well before the 200 chunks ran out
