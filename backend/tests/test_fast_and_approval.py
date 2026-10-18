"""Device test 2026-10-17: the tool-less fast model wrote `<function name="get_system_status">` as its
reply, and a summary written beside a tool call vanished behind the approval card."""
from unittest.mock import MagicMock

import pytest

from app.agent import orchestrator as orch_mod
from app.agent.permissions import ChatMode
from app.agent.tools import registry
from app.config import settings
from test_orchestrator_tool_turns import ScriptedProvider, _text_turn
from test_taint import _call


def _orch():
    o = orch_mod.AgentOrchestrator()
    o.memory_store = MagicMock()
    return o


@pytest.mark.parametrize("text,name", [
    ('<function name="get_system_status"></function>', "get_system_status"),
    ("I'll check. <function name='list_processes'>{\"filter_name\": \"chrome\"}</function>", "list_processes"),
    ("<function=web_search>{\"query\": \"x\"}</function>", "web_search"),
])
def test_function_tags_are_read_as_tool_calls(text, name):
    cleaned, calls = orch_mod.extract_tool_calls_from_text(text)
    assert [c["function"]["name"] for c in calls] == [name]
    assert "<function" not in cleaned


def test_plain_prose_is_not_a_function_tag():
    assert orch_mod.drop_unofferable_calls("GPU: 54°C, VRAM 7.3/8 GB.") == ("GPU: 54°C, VRAM 7.3/8 GB.", False)


@pytest.mark.asyncio
async def test_a_written_out_call_on_a_turn_with_no_tools_is_replaced_not_shown(tmp_path):
    p = ScriptedProvider([_text_turn('I can check your system status. <function name="get_system_status"></function>')])
    events = [ev async for ev in _orch()._run_provider_stream_loop(
        provider=p, session_id="fast-tag", model="fast", system_prompt="sys", tools=[],
        conversation_messages=[{"role": "user", "content": "what's my GPU temperature?"}],
        chat_mode=ChatMode.FREEFORM, workspace_path=tmp_path)]
    done = events[-1]
    assert done["event"] == "done" and done["data"]["replace_content"] is True
    assert done["data"]["response"] == orch_mod._NO_TOOLS_REPLY
    assert not [e for e in events if e["event"] == "tool_start"]  # nothing was offered, nothing ran
    assert "no-tools" in done["data"]["metrics"]["nets"]


@pytest.mark.asyncio
async def test_an_ordinary_reply_is_not_replaced(tmp_path):
    p = ScriptedProvider([_text_turn("Good morning, sir.")])
    events = [ev async for ev in _orch()._run_provider_stream_loop(
        provider=p, session_id="fast-plain", model="fast", system_prompt="sys", tools=[],
        conversation_messages=[{"role": "user", "content": "hey"}], chat_mode=ChatMode.FREEFORM, workspace_path=tmp_path)]
    assert events[-1]["data"]["replace_content"] is False


def test_hardware_questions_are_read_first_only_on_a_tool_less_turn():
    o = _orch()
    q = "what's my GPU temperature / VRAM usage?"
    assert o._should_pre_status(q, [], None)
    assert not o._should_pre_status(q, [registry.get_system_status], None)  # main calls it itself
    assert not o._should_pre_status(q, [], "chat")  # voice mode answers these in code
    assert not o._should_pre_status("what's the temperature outside?", [], None)


@pytest.mark.asyncio
async def test_the_pre_read_runs_the_real_tool(monkeypatch):
    monkeypatch.setitem(registry.TOOL_FUNCTIONS, "get_system_status", lambda **_: "GPU 54°C, VRAM 7.3/8.0 GB")
    item, found = await _orch()._pre_status("pre-status", {"session_id": "pre-status"})
    assert found == "GPU 54°C, VRAM 7.3/8.0 GB" and item["pre_run"] and item["tool"] == "get_system_status"
    assert "LIVE READINGS" in orch_mod._pre_status_block(found)


def test_the_readings_lead_with_just_the_part_asked_about(monkeypatch):
    """Second device test: "GPU temperature?" on Fast pasted the whole line, battery and drives too."""
    from types import SimpleNamespace
    from test_voice_mode import _snap
    monkeypatch.setattr("app.main.awareness_monitor", SimpleNamespace(last_snapshot=_snap()))
    answer = orch_mod._status_sentence("what's my GPU temperature?")
    assert answer == "The GPU is at 3% with 5.1 of 8 gigabytes of VRAM in use, 52 degrees."
    block = orch_mod._pre_status_block("GPU 52°C, battery 80%, C: 120 GB free", answer)
    assert block.splitlines()[1] == f"Answer with: {answer}"
    assert "battery 80%" in block  # still there if the user asks for more
    monkeypatch.setattr("app.main.awareness_monitor", SimpleNamespace(last_snapshot=None))
    assert orch_mod._status_sentence("what's my GPU temperature?") == ""


@pytest.mark.parametrize("q", ["What tools do you have?", "what can you do", "Jarvis, what can you do for me?",
                               "what are your capabilities", "list your tools", "which tools can you use here?"])
def test_capability_questions_are_recognised(q):
    assert orch_mod.asks_capabilities(q)


@pytest.mark.parametrize("q", ["what can you do about the lag?", "what tools do you have for video editing",
                               "which tools should I use to edit video?", "tell me a joke"])
def test_other_questions_are_not_capability_questions(q):
    assert not orch_mod.asks_capabilities(q)


@pytest.mark.asyncio
@pytest.mark.parametrize("picked,expected", [("fast", orch_mod._FAST_PICKED_CAPABILITIES),
                                             (None, orch_mod._AUTO_CAPABILITIES)])
async def test_fast_answers_what_it_can_do_in_code(monkeypatch, picked, expected):
    """Device test 2026-10-18: Bonsai said "I can manage your files, hardware, and local processes"."""
    o = _orch()
    monkeypatch.setattr(o, "_prefer_loaded_slot", lambda d, f, r, m: (d, True, None))
    monkeypatch.setattr(o, "_run_provider_stream_loop", lambda **_: pytest.fail("the model was asked"))
    events = [ev async for ev in o.run_stream("What tools do you have?", session_id="caps", requested_model=picked,
                                              chat_mode="FREEFORM")]
    assert events[-1]["event"] == "done" and events[-1]["data"]["response"] == expected
    roles = [c.kwargs["role"] for c in o.memory_store.append_message.call_args_list]
    assert roles == ["user", "assistant"]


def test_the_fast_note_overrides_the_freeform_file_tools_line():
    assert "whatever the space note says" in orch_mod._FAST_NO_TOOLS_NOTE
    assert "never write a tool call out as text" in orch_mod._FAST_NO_TOOLS_NOTE
    # "what tools do you have?" on Fast listed Main's tools and claimed control of the machine
    assert "Main or Auto has the tools" in orch_mod._FAST_NO_TOOLS_NOTE


@pytest.mark.asyncio
async def test_text_written_beside_a_call_survives_the_approval_card(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "freeform_files_root", str(tmp_path))
    monkeypatch.setitem(registry.TOOL_FUNCTIONS, "web_search", lambda query, **_: f"results for {query}")
    summary_round = [
        {"event": "text_delta", "content": "Summary: Argon has a 1M-token output."},
        {"event": "tool_call", "tool_call": {"id": "c1", "type": "function",
                                             "function": {"name": "web_search", "arguments": {"query": "argon"}}}},
        {"event": "done", "raw": {"content": "Summary: Argon has a 1M-token output.", "reasoning": "", "tool_calls": None}},
    ]
    p = ScriptedProvider([summary_round, _call("c2", "write_file", {"file_path": "Downloads/s.md", "content": "x"})])
    events = [ev async for ev in _orch()._run_provider_stream_loop(
        provider=p, session_id="keep-said", model="main", system_prompt="sys",
        tools=[registry.web_search, registry.write_file],
        conversation_messages=[{"role": "user", "content": "summarise argon and save it in downloads"}],
        chat_mode=ChatMode.FREEFORM, workspace_path=tmp_path,
        tool_context={"chat_mode": "FREEFORM", "session_id": "keep-said", "workspace_path": str(tmp_path)})]
    last = events[-1]
    assert last["event"] == "confirmation_required"
    assert last["data"]["response"].startswith("Summary: Argon has a 1M-token output.")
    assert "Confirmation required" in last["data"]["response"]
    assert not (tmp_path / "Downloads" / "s.md").exists()
