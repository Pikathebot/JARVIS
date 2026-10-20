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
    AgentOrchestrator, _NUDGE_TO_ACT, _NUDGE_TO_ANSWER, _NUDGE_UNRUN_CLAIM, _NUDGE_UNRUN_LOOKUP, _with_system_prompt,
    TurnMetrics, changed_something, claims_unrun_action, looks_like_action, promises_a_lookup, writes_a_call_as_text,
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


def test_main_reasons_every_turn_by_default(monkeypatch):
    """A quick turn served on main keeps thinking on (the user's choice for Ornith, 2026-10-20)."""
    import app.agent.runtime_process_manager as rpm
    orch = _orchestrator()
    monkeypatch.setattr(rpm, "get_runtime_process_manager", lambda: _Runtime("main"))
    decision = RoutingDecision(mode="normal", provider="llama_cpp", model="fast", reason="short turn")
    decision, is_fast, thinking = orch._prefer_loaded_slot(decision, True, None, "forget the previous topic")
    assert (decision.model, is_fast, thinking) == ("main", False, None)
    assert "thinking on" in decision.reason


def test_requests_to_act_never_run_on_the_toolless_fast_model(monkeypatch):
    """The "delete probe.txt" regression, after tools stopped being keyword-picked (2026-10-01):
    the fast model has no tools, so a request to act goes to main even when fast is loaded."""
    from app.config import settings
    monkeypatch.setattr(settings, "main_thinking_every_turn", False)
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
    # 2026-10-16, Defiant Fable 9B: claims without "I", and a promise no tool backed
    ("Bumped version to 1.4.4 and saved.", True),
    ("Done. Saved the summary to todo_summary.txt.", True),
    ("Copied the action items to your clipboard.", True),
    ("worker.log has more ERROR lines (4 vs. 2).\n\nI'll remind you of this in 10 minutes.", True),
    ("I will now delete the drafts folder.", True),
    ("Action items copied to clipboard:\n- Bob: book the venue", True),
    ("The summary is saved to todo_summary.txt.", True),
    ("Changes are saved to disk when the buffer flushes.", False),
    # 2026-10-18 notes eval: a to-do "ticked off" after only tasks(list), or no call at all
    ("Done. Marked \"Buy milk\" as complete.", True),
    ("I have recorded that you bought the milk.", True),
    ("Ticked off: Buy milk.", True),
    ("Exams are marked out of 100.", False),
    ("I've added \"milk\" to your to-do list.", True),
    ("I've added error handling to the function:", False),
    # a whole reply of "Added." (eval todo-add 2026-10-19); "Added in 3.12" opening prose is not a claim
    ("Added.", True),
    ("Done, sir.", True),
    ("Added in Python 3.12, the flag controls the JIT.", False),
    # passive claims after a launch_app that had failed (device 2026-10-20)
    ("“File Explorer” has been launched.", True),
    ("File Explorer is now open.", True),
    ("Python has been around since 1991.", False),
    # offers, questions and plain facts are not claims
    ("I'll open it if you want.", False),
    ("Want me to set a reminder? I'll remind you whenever you say.", False),
    ("Should I save it?", False),
    ("I'll need the exact file name first.", False),
    ("Python 3.13 was released in October 2024.", False),
    ("The falls are in Kanniyakumari district; the river was dammed in 1906.", False),
])
def test_claims_unrun_action(text, claims):
    assert claims_unrun_action(text) is claims


def test_only_successful_state_changing_tools_back_a_claim():
    assert not changed_something([])
    assert not changed_something([{"tool": "read_file", "status": "success"},
                                  {"tool": "web_search", "status": "success"},
                                  {"tool": "reminders", "args": {"action": "list"}, "status": "success"}])
    assert not changed_something([{"tool": "write_file", "status": "error"},
                                  {"tool": "kill_process", "status": "timeout"}])
    assert changed_something([{"tool": "read_file"}, {"tool": "write_file"}])  # non-stream items carry no status
    assert changed_something([{"tool": "reminders", "args": {"action": "cancel"}, "status": "success"}])
    assert changed_something([{"tool": "remind_me", "status": "success"}])


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


def _read_turn(path):
    return [
        {"event": "tool_call", "tool_call": {"id": "r1", "type": "function",
                                              "function": {"name": "read_file", "arguments": {"file_path": path}}}},
        {"event": "done", "raw": {"content": "", "reasoning": "", "tool_calls": None}},
    ]


@pytest.mark.asyncio
async def test_a_claim_after_only_reads_is_still_sent_back(tmp_path):
    # 2026-10-16: read config.json, then "Bumped ... and saved." with no write. Any tool used to
    # count as backing; only a state-changing one does now.
    (tmp_path / "config.json").write_text('{"version": "1.4.3"}', encoding="utf-8")
    provider = ScriptedProvider([
        _read_turn("config.json"),
        _text_turn("Bumped version to 1.4.4 and saved."),
        _text_turn("I have not saved it yet."),
    ])
    orch = _orchestrator()
    orch.memory_store = MagicMock()
    events = [ev async for ev in orch._run_provider_stream_loop(
        provider=provider, session_id="claim-read-test", model="main", system_prompt="sys",
        conversation_messages=[{"role": "user", "content": "bump the patch version in config.json"}],
        tools=[read_file, delete_file], chat_mode=ChatMode.WORKSPACE, workspace_path=tmp_path,
    )]
    assert events[-1]["event"] == "done"
    assert len(provider.calls) == 3
    assert provider.calls[2]["messages"][-1] == {"role": "user", "content": _NUDGE_UNRUN_CLAIM}


@pytest.mark.asyncio
async def test_reasoning_only_after_tools_asks_for_the_answer(tmp_path):
    # The answer sat in the reasoning after a read; "you stopped before doing anything" was false
    # and drew "I apologize for the confusion".
    (tmp_path / "story.md").write_text("The Entity controls 0.001% of the population.", encoding="utf-8")
    provider = ScriptedProvider([
        _read_turn("story.md"),
        [{"event": "reasoning_delta", "content": "It controls 0.001%."},
         {"event": "done", "raw": {"content": "", "reasoning": "It controls 0.001%.", "tool_calls": None}}],
        _text_turn("The Entity controls 0.001% of the population."),
    ])
    orch = _orchestrator()
    orch.memory_store = MagicMock()
    events = [ev async for ev in orch._run_provider_stream_loop(
        provider=provider, session_id="answer-nudge-test", model="main", system_prompt="sys",
        conversation_messages=[{"role": "user", "content": "how much does the Entity control?"}],
        tools=[read_file], chat_mode=ChatMode.WORKSPACE, workspace_path=tmp_path,
    )]
    assert events[-1]["event"] == "done"
    assert provider.calls[2]["messages"][-1] == {"role": "user", "content": _NUDGE_TO_ANSWER}
    m = events[-1]["data"]["metrics"]  # the reply's metrics line (PLAN 4.13 #9)
    assert (m["rounds"], m["tools"], m["nets"]) == (3, 1, ["nudged"])


def test_turn_metrics_from_llama_server_timings():
    m = TurnMetrics()
    m.add_round({"ttft_ms": 412.6, "timings": {"cache_n": 1400, "prompt_n": 230, "predicted_n": 40, "predicted_ms": 1500.0}})
    m.add_round({"ttft_ms": 90.0, "timings": {"cache_n": 1700, "prompt_n": 60, "predicted_n": 14, "predicted_ms": 500.0}})
    m.nets.append("claim-checked")
    p = m.payload([{"tool": "read_file"}])
    # first-token time and prompt / cache from round one; speed over every round
    assert p == {"rounds": 2, "tools": 1, "ttft_ms": 413, "tokens_per_second": 27.0,
                 "prompt_tokens": 1630, "cached_tokens": 1400, "nets": ["claim-checked"]}
    assert TurnMetrics().payload([])["tokens_per_second"] is None  # a provider without timings


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
    from app.config import settings
    monkeypatch.setattr(settings, "main_thinking_every_turn", False)  # the pre-Ornith behaviour
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
    assert "execute_command" in names("WORKSPACE") and "execute_command" not in names("FREEFORM")
    assert "launch_app" in names("FREEFORM") and "web_search" in names("FREEFORM")



@pytest.mark.parametrize("text,promises", [
    # device 2026-10-19: three announcements, no call, until the user said "use tool call"
    ("I can list your calendar events for each month, but the calendar API only supports querying by "
     "\"this year\" or by individual months in a single call. I'll fetch the full year's view in one pass.", True),
    ("Fetching your full-year calendar.", True),
    ("Checking your full-year calendar.", True),
    ("Adding \"need to buy bread\" to your tasks list.", True),
    ("Let me check your calendar.", True),
    # offers, plain answers and long answers that merely mention checking are not
    ("Shall I check the rest of the week?", False),
    ("I'll check it if you want.", False),
    ("Nothing on the calendar for today.", False),
    ("Checking the docs, the flag was added in 3.12. " + "More detail follows here. " * 12, False),
])
def test_promises_a_lookup(text, promises):
    assert promises_a_lookup(text) is promises


@pytest.mark.asyncio
async def test_an_announced_lookup_with_no_call_is_sent_back_once(tmp_path):
    (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")
    provider = ScriptedProvider([
        _text_turn("Fetching your notes."),
        _read_turn("notes.txt"),
        _text_turn("It says hello."),
    ])
    orch = _orchestrator()
    orch.memory_store = MagicMock()

    events = [ev async for ev in orch._run_provider_stream_loop(
        provider=provider, session_id="lookup-test", model="main", system_prompt="sys",
        conversation_messages=[{"role": "user", "content": "what's in notes.txt?"}],
        tools=[read_file], chat_mode=ChatMode.WORKSPACE, workspace_path=tmp_path,
    )]

    assert events[-1]["event"] == "done" and len(provider.calls) == 3
    assert provider.calls[1]["messages"][-1] == {"role": "user", "content": _NUDGE_UNRUN_LOOKUP}


@pytest.mark.parametrize("text,names,writes", [
    ("calendar(action='add', title='Dentist appointment', when='next Tuesday 3pm')", ["calendar", "tasks"], True),
    ("Sure: tasks(action=\"add\", text=\"milk\")", ["calendar", "tasks"], True),
    ("Use calendar(action='add') to add events.", ["tasks"], False),  # not an offered tool
    ("The function print(x) takes x.", ["calendar"], False),
    ("Call calendar() with no arguments.", ["calendar"], False),
    ("np.calendar(year=2026) is numpy's", ["calendar"], False),
])
def test_writes_a_call_as_text(text, names, writes):
    assert writes_a_call_as_text(text, names) is writes
