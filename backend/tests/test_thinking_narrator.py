"""The live "what it's doing" line while the model thinks (PLAN 4.23)."""
import asyncio
from typing import Any

import pytest

import app.agent.orchestrator as orchestrator_module
import app.agent.thinking_narrator as tn
from app.agent.llamacpp_provider import _main_slot
from app.agent.permissions import ChatMode
from app.agent.thinking_narrator import ThinkingNarrator, clean_line, narrator_for
from app.agent.tools.registry import read_file
from app.config import settings
from app.memory.ephemeral import EphemeralMemoryStore
from tests.test_orchestrator_tool_turns import ScriptedProvider, _orchestrator


@pytest.mark.parametrize("raw, line", [
    ("Checking the calendar for Friday.", "Checking the calendar for Friday"),
    ('"weighing two ways to word it"\n\nmore text', "Weighing two ways to word it"),
    ("<think>hmm</think>Reading the file", "Reading the file"),
    ("", ""),
    ("<tool_call>", ""),
])
def test_clean_line(raw, line):
    assert clean_line(raw) == line


def test_a_long_line_is_cut_at_a_word():
    line = clean_line("Comparing the three candidate approaches to the scheduling problem in detail")
    assert len(line) <= tn.MAX_LINE_CHARS + 1 and line.endswith("…") and " " in line


def test_chat_requests_are_pinned_to_slot_zero(monkeypatch):
    monkeypatch.setattr(settings, "llama_parallel", 2)
    assert _main_slot() == {"id_slot": 0}
    monkeypatch.setattr(settings, "llama_parallel", 1)
    assert _main_slot() == {}


def test_no_narrator_without_a_second_slot_or_thinking(monkeypatch):
    class Llama:
        name, base_url = "llama_cpp", "http://127.0.0.1:8001"

    monkeypatch.setattr(settings, "thinking_narrator_enabled", True)
    monkeypatch.setattr(settings, "llama_parallel", 2)
    assert narrator_for(Llama(), "main", None) is not None
    assert narrator_for(Llama(), "main", False) is None
    assert narrator_for(object(), "main", None) is None
    monkeypatch.setattr(settings, "llama_parallel", 1)
    assert narrator_for(Llama(), "main", None) is None


@pytest.mark.asyncio
async def test_narrator_asks_slot_one_without_cache(monkeypatch):
    sent: list[dict[str, Any]] = []

    class Response:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "Looking up the weather."}}]}

    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json):
            sent.append(json)
            return Response()

    monkeypatch.setattr(tn.httpx, "AsyncClient", Client)
    narrator = ThinkingNarrator("http://x", "main", interval_s=0)
    monkeypatch.setattr(tn, "FIRST_AFTER_S", 0)
    narrator.feed("The user wants the weather in Chennai. " * 5)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert narrator.take() == "Looking up the weather"
    body = sent[0]
    assert body["id_slot"] == 1 and body["cache_prompt"] is False
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    # The same line twice is shown once; nothing after stop.
    narrator.feed("more thinking about Chennai " * 10)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert narrator.take() is None
    narrator.stop()
    narrator.feed("x" * 500)
    assert narrator.take() is None and len(sent) == 2


class _FakeNarrator:
    def __init__(self):
        self.fed, self.stopped = "", False

    def feed(self, delta):
        self.fed += delta

    def take(self):
        return "Reading the notes" if self.fed and not self.stopped else None

    def stop(self):
        self.stopped = True


@pytest.mark.asyncio
async def test_turn_streams_the_line_and_keeps_each_rounds_thinking(tmp_path, monkeypatch):
    (tmp_path / "notes.md").write_text("Buy milk.", encoding="utf-8")
    fake = _FakeNarrator()
    monkeypatch.setattr(orchestrator_module, "narrator_for", lambda *a: fake)
    provider = ScriptedProvider([
        [
            {"event": "reasoning_delta", "content": "I should read notes.md first."},
            {"event": "tool_call", "tool_call": {"id": "r1", "type": "function",
                                                  "function": {"name": "read_file", "arguments": {"file_path": "notes.md"}}}},
            {"event": "done", "raw": {"content": "", "reasoning": "", "tool_calls": None}},
        ],
        [
            {"event": "reasoning_delta", "content": "It says buy milk."},
            {"event": "text_delta", "content": "Your note says: buy milk."},
            {"event": "done", "raw": {"content": "Your note says: buy milk.", "reasoning": "", "tool_calls": None}},
        ],
    ])
    orch = _orchestrator()
    orch.memory_store = EphemeralMemoryStore(orch.memory_store)
    orch.memory_store.get_or_create_session("narrate")
    events = [ev async for ev in orch._run_provider_stream_loop(
        provider=provider, session_id="narrate", model="main", system_prompt="sys",
        conversation_messages=[{"role": "user", "content": "what's in notes.md?"}],
        tools=[read_file], chat_mode=ChatMode.WORKSPACE, workspace_path=tmp_path,
    )]
    assert {"event": "thinking_status", "data": {"text": "Reading the notes"}} in events
    assert events[-1]["event"] == "done" and fake.stopped

    stored = orch.memory_store.get_messages("narrate")
    tool_row = next(m for m in stored if m["role"] == "tool")
    answer = next(m for m in stored if m["role"] == "assistant")
    assert tool_row["reasoning_content"] == "I should read notes.md first."
    assert answer["reasoning_content"] == "It says buy milk."


def test_two_slots_share_one_kv_pool(monkeypatch, tmp_path):
    # Without --kv-unified the server split 32k into two 16k slots and refused a 16.5k thread
    # (2026-10-24); with it each slot has the whole context (measured n_ctx_slot = n_ctx).
    from pathlib import Path

    from app.agent.runtime_process_manager import RuntimeProcessManager

    pm = RuntimeProcessManager()
    monkeypatch.setattr(settings, "llama_parallel", 2)
    cmd = pm._build_cmd(Path("llama-server.exe"), tmp_path / "m.gguf", "main", 32768, None)
    assert cmd[cmd.index("--parallel") + 1] == "2" and "--kv-unified" in cmd
    assert cmd[cmd.index("--ctx-size") + 1] == "32768"
    monkeypatch.setattr(settings, "llama_parallel", 1)
    assert "--kv-unified" not in pm._build_cmd(Path("llama-server.exe"), tmp_path / "m.gguf", "main", 32768, None)


def test_thinking_has_a_budget_on_the_server(monkeypatch, tmp_path):
    from pathlib import Path

    from app.agent.runtime_process_manager import RuntimeProcessManager

    pm = RuntimeProcessManager()
    monkeypatch.setattr(settings, "llama_chat_template_kwargs", '{"enable_thinking":true}')
    monkeypatch.setattr(settings, "llama_reasoning_budget", 4096)
    cmd = pm._build_cmd(Path("llama-server.exe"), tmp_path / "m.gguf", "main", 32768, None)
    assert cmd[cmd.index("--reasoning-budget") + 1] == "4096" and "--reasoning-budget-message" in cmd
    monkeypatch.setattr(settings, "llama_reasoning_budget", 0)
    assert "--reasoning-budget" not in pm._build_cmd(Path("llama-server.exe"), tmp_path / "m.gguf", "main", 32768, None)
