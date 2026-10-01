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
from app.agent.orchestrator import (
    AgentOrchestrator, _NUDGE_TO_ACT, _NUDGE_UNRUN_CLAIM, _with_system_prompt, claims_unrun_action,
    looks_like_action,
)
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
        self, messages, model=None, tools=None, temperature=None, profile="general", timeout=None, thinking=None
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
    return AgentOrchestrator()


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


def test_requests_to_act_never_run_on_the_toolless_fast_model(monkeypatch):
    """The "delete probe.txt" regression, after tools stopped being keyword-picked (2026-10-01):
    the fast model has no tools, so a request to act goes to main even when fast is loaded."""
    import app.agent.runtime_process_manager as rpm
    orch = _orchestrator()
    monkeypatch.setattr(rpm, "get_runtime_process_manager", lambda: _Runtime("fast"))

    def fast() -> RoutingDecision:
        return RoutingDecision(mode="normal", provider="llama_cpp", model="fast", reason="short turn")

    decision, is_fast, thinking = orch._prefer_loaded_slot(fast(), True, None, "delete probe.txt")
    assert (decision.model, is_fast, thinking) == ("main", False, False)
    assert "no tools" in decision.reason
    # Chat stays on the loaded fast model; an explicit choice of fast is respected.
    assert orch._prefer_loaded_slot(fast(), True, None, "what's a haiku?")[1] is True
    assert orch._prefer_loaded_slot(fast(), True, "fast", "delete probe.txt")[1] is True


@pytest.mark.parametrize("text, acting", [
    ("open discord", True), ("mute", True), ("delete probe.txt", True), ("what did I change in git", True),
    ("hi", False), ("what's a haiku?", False), ("thanks, that helps", False),
])
def test_looks_like_action(text, acting):
    assert looks_like_action(text) is acting


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


# --- a reply that claims an action no tool performed (2026-09-29 "I attempted to open Qwen") ---

@pytest.mark.parametrize("text, claims", [
    ("I attempted to open Qwen, but the application isn't currently available.", True),
    ("I've opened Discord for you.", True),
    ("I have deleted probe.txt.", True),
    ("Done -- I muted the volume.", True),
    ("Python passes object references by value.", False),
    ("I can open it if you tell me the exact name.", False),
    ("I haven't opened anything yet.", False),
])
def test_claims_unrun_action(text, claims):
    assert claims_unrun_action(text) is claims


def _text_turn(text):
    return [
        {"event": "text_delta", "content": text},
        {"event": "done", "raw": {"content": text, "reasoning": "", "tool_calls": None}},
    ]


@pytest.mark.asyncio
async def test_a_claimed_action_with_no_tool_call_is_sent_back_once(tmp_path):
    provider = ScriptedProvider([
        _text_turn("I attempted to open Qwen, but it isn't available."),
        _text_turn("I attempted to open it again."),  # a second claim is not sent back again
    ])
    orch = _orchestrator()
    orch.memory_store = MagicMock()

    events = [ev async for ev in orch._run_provider_stream_loop(
        provider=provider, session_id="claim-test", model="main", system_prompt="sys",
        conversation_messages=[{"role": "user", "content": "open qwen"}],
        tools=[read_file, delete_file], chat_mode=ChatMode.WORKSPACE, workspace_path=tmp_path,
    )]

    assert events[-1]["event"] == "done"
    assert len(provider.calls) == 2 and provider.calls[1]["tools"]
    second = provider.calls[1]["messages"]
    assert second[-1] == {"role": "user", "content": _NUDGE_UNRUN_CLAIM}
    assert "attempted to open Qwen" in second[-2]["content"]


@pytest.mark.asyncio
async def test_plain_answers_and_toolless_turns_are_not_checked(tmp_path):
    for tools, text in (([read_file], "Python passes references by value."),
                        (None, "I opened the door to new ideas.")):
        provider = ScriptedProvider([_text_turn(text)])
        orch = _orchestrator()
        orch.memory_store = MagicMock()
        events = [ev async for ev in orch._run_provider_stream_loop(
            provider=provider, session_id="claim-test-2", model="main", system_prompt="sys",
            conversation_messages=[{"role": "user", "content": "hi"}],
            tools=tools, chat_mode=ChatMode.WORKSPACE, workspace_path=tmp_path,
        )]
        assert events[-1]["event"] == "done"
        assert len(provider.calls) == 1


# --- a fast verdict never swaps models; it runs on whatever is loaded, thinking off on main ---

class _Runtime:
    def __init__(self, kind):
        self.current_model_kind = kind


def test_fast_verdict_stays_on_loaded_main_with_thinking_off(monkeypatch):
    import app.agent.runtime_process_manager as rpm
    orch = _orchestrator()

    def fast():
        return RoutingDecision(mode="normal", provider="llama_cpp", model="fast", reason="short turn")

    monkeypatch.setattr(rpm, "get_runtime_process_manager", lambda: _Runtime("main"))
    decision, is_fast, thinking = orch._prefer_loaded_slot(fast(), True, None)
    assert (decision.model, is_fast, thinking) == ("main", False, False)
    assert "thinking off" in decision.reason

    # Nothing loaded yet: main is the right thing to load, since the next tool turn needs it anyway.
    monkeypatch.setattr(rpm, "get_runtime_process_manager", lambda: _Runtime(None))
    decision, is_fast, thinking = orch._prefer_loaded_slot(fast(), True, None)
    assert (decision.model, is_fast, thinking) == ("main", False, False)

    # The 4B is what is loaded: use it as-is (it launches with thinking off already).
    monkeypatch.setattr(rpm, "get_runtime_process_manager", lambda: _Runtime("fast"))
    decision, is_fast, thinking = orch._prefer_loaded_slot(fast(), True, None)
    assert (decision.model, is_fast, thinking) == ("fast", True, None)

    # An explicit model choice, a main verdict, and a cloud route are all left alone.
    monkeypatch.setattr(rpm, "get_runtime_process_manager", lambda: _Runtime("main"))
    decision, is_fast, thinking = orch._prefer_loaded_slot(fast(), True, "fast")
    assert (decision.model, is_fast, thinking) == ("fast", True, None)
    main = RoutingDecision(mode="normal", provider="llama_cpp", model="main", reason="")
    assert orch._prefer_loaded_slot(main, False, None) == (main, False, None)
    cloud = RoutingDecision(mode="heavy", provider="openrouter", model="x", reason="")
    assert orch._prefer_loaded_slot(cloud, True, None) == (cloud, True, None)


def test_the_offered_tool_set_is_fixed_per_space():
    from app.agent.tools.registry import tools_for_space
    names = lambda mode: [t.__name__ for t in tools_for_space(mode)]
    # Byte-stable prompt prefix: identical list, same order, every turn.
    assert names("WORKSPACE") == names("WORKSPACE") == names("SYSTEM")
    assert "read_file" in names("WORKSPACE") and "read_file" not in names("FREEFORM")
    assert "launch_app" in names("FREEFORM") and "web_search" in names("FREEFORM")

