"""Loading the chat model ahead of a message (2026-10-21): POST /models/warm starts the main model in
the background for the Load model button and the first keystroke or mic tap, and never waits or
overrides -- Game mode, voice mode and a busy PC hold it."""
import asyncio

import pytest
from fastapi.testclient import TestClient

from app.remote import remote_route_allowed


class FakeManager:
    def __init__(self, serving=None):
        self.current_model_kind = serving
        self.loads = []
        self.release = asyncio.Event()

    async def health_check(self, timeout=2.0):
        return self.current_model_kind is not None

    async def ensure_running(self, model_kind="main"):
        self.loads.append(model_kind)
        await self.release.wait()
        self.current_model_kind = model_kind
        return True


@pytest.fixture
def warm(monkeypatch):
    import app.main as main

    manager = FakeManager()
    monkeypatch.setattr(main, "get_runtime_process_manager", lambda: manager)
    monkeypatch.setattr(main, "_warm_task", None)
    monkeypatch.setattr(main.game_mode, "active", False)

    async def healthy(timeout_seconds=3.0):
        return True, None

    monkeypatch.setattr(main.governor, "wait_until_healthy", healthy)
    return main, manager


def test_the_phone_may_warm_but_not_pick_a_model():
    assert remote_route_allowed("POST", "/models/warm")
    assert not remote_route_allowed("POST", "/models/load")


@pytest.mark.asyncio
async def test_warming_starts_one_background_load_and_returns_at_once(warm):
    main, manager = warm
    assert await main.warm_model() == {"state": "loading"}
    assert await main.warm_model() == {"state": "loading"}  # a second tap joins the load in flight
    await asyncio.sleep(0)
    assert manager.loads == ["main"]
    manager.release.set()
    await main._warm_task
    assert await main.warm_model() == {"state": "ready", "model": "main"}


@pytest.mark.asyncio
async def test_whatever_is_serving_is_left_alone(warm):
    main, manager = warm
    manager.current_model_kind = "fast"
    assert await main.warm_model() == {"state": "ready", "model": "fast"}
    assert manager.loads == []


@pytest.mark.asyncio
async def test_game_mode_holds_the_warm_up(warm, monkeypatch):
    main, manager = warm
    monkeypatch.setattr(main.game_mode, "active", True)
    assert (await main.warm_model())["state"] == "held"
    assert manager.loads == []


@pytest.mark.asyncio
async def test_a_busy_pc_holds_the_warm_up_without_waiting(warm, monkeypatch):
    main, manager = warm
    waited = []

    async def busy(timeout_seconds=3.0):
        waited.append(timeout_seconds)
        return False, "VRAM above threshold"

    monkeypatch.setattr(main.governor, "wait_until_healthy", busy)
    monkeypatch.setattr(main.settings, "governor_enabled", True)
    assert await main.warm_model() == {"state": "held", "reason": "VRAM above threshold"}
    assert waited == [0] and manager.loads == []


def test_the_endpoint_answers(warm):
    main, manager = warm
    manager.current_model_kind = "main"
    assert TestClient(main.app).post("/models/warm").json() == {"state": "ready", "model": "main"}
