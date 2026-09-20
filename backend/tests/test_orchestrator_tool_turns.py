"""
The "delete probe.txt" regression: a short command landed on the fast slot, the prompt shipped
twice, and the model announced a deletion it never performed. These pin the three fixes:
one system message, guarded tools route to main, and a reasoning-only stop gets one nudge.
"""
from typing import Any, AsyncIterator
from unittest.mock import MagicMock

import pytest

from app.agent.model_provider import ModelProvider
from app.agent.model_router import RoutingDecision
from app.agent.orchestrator import AgentOrchestrator, _NUDGE_TO_ACT, _with_system_prompt
from app.agent.permissions import ChatMode
from app.agent.tools.registry import delete_file, read_file, web_search


class ScriptedProvider(ModelProvider):
    """Plays back one scripted event list per stream_chat call and records what it was sent."""

    def __init__(self, turns: list[list[dict[str, Any]]]):
        self.turns = turns
        self.calls: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return "llama_cpp"

    async def stream_chat(
        self, messages, model=None, tools=None, temperature=None, profile="general", timeout=None
    ) -> AsyncIterator[dict[str, Any]]:
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools})
        events = self.turns[len(self.calls) - 1] if len(self.calls) <= len(self.turns) else []
        for ev in events:
            yield ev

    async def chat(self, *args, **kwargs) -> dict[str, Any]:
        self.calls.append({"chat": True, "tools": kwargs.get("tools")})
        return {"message": {"role": "assistant", "content": "prose fallback"}}

    async def health_check(self) -> bool:
        return True

    async def model_info(self) -> dict[str, Any]:
        return {}

    async def list_models(self) -> list[str]:
        return []

    async def unload_model(self, model=None) -> bool:
        return True


def _orchestrator() -> AgentOrchestrator:
    engine = MagicMock()
    engine.sanitize_text = lambda t: t
    return AgentOrchestrator(tts_engine=engine, voice_output_enabled=False)


def test_with_system_prompt_sends_the_prompt_once():
    convo = [
        {"role": "system", "content": "assembled prompt"},
        {"role": "user", "content": "delete probe.txt"},
    ]
    out = _with_system_prompt("assembled prompt", convo)
    assert [m["role"] for m in out] == ["system", "user"]
    assert out[0]["content"] == "assembled prompt"
    # A conversation without its own system message still gets exactly one.
    assert [m["role"] for m in _with_system_prompt("p", convo[1:])] == ["system", "user"]


def test_guarded_tools_route_to_main_unless_model_was_chosen():
    orch = _orchestrator()

    def fast() -> RoutingDecision:
        return RoutingDecision(mode="normal", provider="llama_cpp", model="fast", reason="short turn")

    decision, is_fast = orch._prefer_capable_slot(fast(), True, [read_file, delete_file], None)
    assert (decision.model, is_fast) == ("main", False)
    assert "delete_file" in decision.reason

    decision, is_fast = orch._prefer_capable_slot(fast(), True, [read_file, web_search], None)
    assert (decision.model, is_fast) == ("fast", True)

    decision, is_fast = orch._prefer_capable_slot(fast(), True, [delete_file], "fast")
    assert (decision.model, is_fast) == ("fast", True)

    cloud = RoutingDecision(mode="heavy", provider="openrouter", model="x", reason="")
    decision, _ = orch._prefer_capable_slot(cloud, True, [delete_file], None)
    assert decision.model == "x"


@pytest.mark.asyncio
async def test_reasoning_only_stop_is_nudged_once_then_acts(tmp_path):
    provider = ScriptedProvider([
        # Turn 1: the model thinks it through and stops without calling anything.
        [
            {"event": "reasoning_delta", "content": "Found it. Now I'll delete the file."},
            {"event": "done", "raw": {"content": "", "reasoning": "Found it. Now I'll delete the file.", "tool_calls": None}},
        ],
        # Turn 2 (after the nudge): the call arrives and is gated for confirmation.
        [
            {"event": "tool_call", "tool_call": {"id": "c1", "type": "function", "function": {"name": "delete_file", "arguments": {"file_path": "probe.txt"}}}},
            {"event": "done", "raw": {"content": "", "reasoning": "", "tool_calls": None}},
        ],
    ])
    orch = _orchestrator()

    events = []
    async for ev in orch._run_provider_stream_loop(
        provider=provider,
        session_id="nudge-test",
        conversation_messages=[{"role": "user", "content": "delete probe.txt"}],
        tools=[read_file, delete_file],
        model="main",
        system_prompt="sys",
        chat_mode=ChatMode.WORKSPACE,
        workspace_path=tmp_path,
    ):
        events.append(ev)

    kinds = [e["event"] for e in events]
    assert kinds[-1] == "confirmation_required"
    assert "token" not in kinds, "reasoning must not be shown as the reply"
    pending = events[-1]["data"]["pending_confirmations"]
    assert [p["tool"] for p in pending] == ["delete_file"]

    assert len(provider.calls) == 2 and all(c.get("tools") for c in provider.calls)
    second = provider.calls[1]["messages"]
    assert second[-1] == {"role": "user", "content": _NUDGE_TO_ACT}
    assert second[-2]["role"] == "assistant" and "delete the file" in second[-2]["content"]


@pytest.mark.asyncio
async def test_reasoning_only_twice_falls_back_to_prose(tmp_path):
    reasoning_only = [
        {"event": "reasoning_delta", "content": "hmm"},
        {"event": "done", "raw": {"content": "", "reasoning": "hmm", "tool_calls": None}},
    ]
    provider = ScriptedProvider([reasoning_only, reasoning_only])
    orch = _orchestrator()
    orch.memory_store = MagicMock()

    events = []
    async for ev in orch._run_provider_stream_loop(
        provider=provider,
        session_id="nudge-test-2",
        conversation_messages=[{"role": "user", "content": "delete probe.txt"}],
        tools=[read_file, delete_file],
        model="main",
        system_prompt="sys",
        chat_mode=ChatMode.WORKSPACE,
        workspace_path=tmp_path,
    ):
        events.append(ev)

    assert events[-1]["event"] == "done"
    assert events[-1]["data"]["response"] == "prose fallback"
    # Nudged once, then the no-tools synthesis -- never a third streamed attempt.
    assert [("chat" in c) for c in provider.calls] == [False, False, True]
