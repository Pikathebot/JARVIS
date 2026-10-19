"""Game mode (PLAN 4.16b): a pre-launch switch that unloads everything and keeps it unloaded."""

from typing import Any, AsyncIterator
from unittest.mock import MagicMock

import httpx
import pytest

from app.agent.model_provider import ModelProvider
from app.agent.orchestrator import AgentOrchestrator
from app.agent.runtime_process_manager import RuntimeProcessManager
from app.governor import game_mode as gm
from app.governor.game_mode import GameMode, command


@pytest.fixture(autouse=True)
def _game_mode_off():
    gm.game_mode.active = False
    yield
    gm.game_mode.active = False
    gm.game_mode.since = None


@pytest.mark.parametrize("text, expected", [
    ("game mode on", "on"),
    ("Turn on game mode", "on"),
    ("turn game mode on please", "on"),
    ("Jarvis, start gaming mode.", "on"),
    ("hey jarvis enable game mode now", "on"),
    ("game mode off", "off"),
    ("turn off game mode", "off"),
    ("Exit game mode!", "off"),
    ("I’m done gaming", "off"),
    ("what is game mode?", None),
    ("does game mode turn off the model", None),
    ("I was in game mode yesterday and it broke the whole evening for me, honestly", None),
    ("open steam", None),
    ("", None),
])
def test_command(text, expected):
    assert command(text) == expected


def _wired(release_result=("the chat model",), vram=(6000.0, 1000.0)):
    mode = GameMode()
    calls: list[str] = []
    readings = list(vram)

    async def release():
        calls.append("release")
        return list(release_result)

    mode.configure(pause=lambda: calls.append("pause"), resume=lambda: calls.append("resume"),
                   release=release, vram_used_mb=lambda: readings.pop(0) if readings else None)
    return mode, calls


@pytest.mark.asyncio
async def test_enter_pauses_then_releases_and_leave_resumes():
    mode, calls = _wired()
    status = await mode.enter()
    assert calls == ["pause", "release"]
    assert status["active"] and status["stopped"] == ["the chat model"] and status["freed_vram_mb"] == 5000.0
    await mode.enter()  # already on: nothing runs twice
    assert calls == ["pause", "release"]
    status = await mode.leave()
    assert calls == ["pause", "release", "resume"] and not status["active"]


@pytest.mark.asyncio
async def test_a_failed_release_still_leaves_game_mode_on():
    mode = GameMode()

    async def boom():
        raise RuntimeError("stop failed")

    mode.configure(pause=lambda: None, resume=lambda: None, release=boom, vram_used_mb=lambda: None)
    status = await mode.enter()
    assert status["active"] and status["stopped"] == [] and status["freed_vram_mb"] is None


@pytest.mark.asyncio
async def test_replies_in_code():
    mode, _ = _wired()
    assert await mode.reply_for("what's the weather") is None  # off: the model answers
    assert await mode.reply_for("game mode off") == "Game mode is already off."
    on = await mode.reply_for("game mode on")
    assert mode.active and on.startswith("Game mode on") and "4.9 GB" in on
    assert "no model is loaded" in await mode.reply_for("what's the weather")
    assert await mode.reply_for("game mode on") == "Game mode is already on."
    assert (await mode.reply_for("turn off game mode")).startswith("Game mode off")
    assert not mode.active


@pytest.mark.asyncio
async def test_nothing_launches_a_model_while_it_is_on():
    gm.game_mode.active = True
    with pytest.raises(RuntimeError, match="Game mode is on"):
        await RuntimeProcessManager().ensure_running("main")
    assert gm.blocked() == gm.BLOCKED


class _NeverCalled(ModelProvider):
    def __init__(self):
        self.calls = 0

    @property
    def name(self) -> str:
        return "llama_cpp"

    async def stream_chat(self, *args, **kwargs) -> AsyncIterator[dict[str, Any]]:
        self.calls += 1
        if False:
            yield {}

    async def chat(self, *args, **kwargs) -> dict[str, Any]:
        self.calls += 1
        return {"message": {"role": "assistant", "content": "x"}}

    async def health_check(self) -> bool:
        return True

    async def model_info(self) -> dict[str, Any]:
        return {}

    async def list_models(self) -> list[str]:
        return []

    async def unload_model(self, model=None) -> bool:
        return True


@pytest.mark.asyncio
async def test_a_turn_in_game_mode_never_reaches_the_model():
    gm.game_mode.active = True
    provider = _NeverCalled()
    orch = AgentOrchestrator(provider=provider)
    orch.memory_store = MagicMock()

    events = [ev async for ev in orch.run_stream(user_message="summarise my inbox", session_id="g1",
                                                 chat_mode="FREEFORM")]
    assert [e["event"] for e in events] == ["token", "done"]
    assert "no model is loaded" in events[-1]["data"]["response"]
    assert provider.calls == 0
    stored = [c.kwargs["role"] for c in orch.memory_store.append_message.call_args_list]
    assert stored == ["user", "assistant"]

    result = await orch.run(user_message="hello", session_id="g1", chat_mode="FREEFORM")
    assert "no model is loaded" in result.response and provider.calls == 0


@pytest.mark.asyncio
async def test_endpoints_and_chat_while_on(monkeypatch):
    from app.main import app, governor

    released: list[str] = []

    async def fake_release():
        released.append("x")
        return ["the chat model"]

    monkeypatch.setattr(gm.game_mode, "_release", fake_release)
    monkeypatch.setattr(gm.game_mode, "_vram_used_mb", lambda: None)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.post("/api/game-mode", json={"enabled": True})
            assert res.json()["active"] is True and released == ["x"]
            assert governor.status.value == "PAUSED"
            assert (await ac.get("/health")).json()["game_mode"] is True
            res = await ac.post("/models/load", json={"model_name": "main"})
            assert res.status_code == 409 and "Game mode is on" in res.json()["detail"]

            # Answered at once in code: no 429 from the paused governor, no model.
            res = await ac.post("/chat/stream", json={"message": "what's on my calendar", "session_id": "gm-http",
                                                      "chat_mode": "FREEFORM", "ephemeral": True})
            assert res.status_code == 200 and "no model is loaded" in res.text

            res = await ac.post("/chat/stream", json={"message": "game mode off", "session_id": "gm-http",
                                                      "chat_mode": "FREEFORM", "ephemeral": True})
            assert "Game mode off" in res.text
            assert (await ac.get("/api/game-mode")).json()["active"] is False
            assert governor.status.value != "PAUSED"
    finally:
        governor.force_resume()
