import pytest

from app.agent.tools import app_control
from app.agent.tools.registry import TOOL_FUNCTIONS, TOOL_SCHEMAS
from app.governor.game_mode import game_mode, turn_in_progress
from app.governor.process_watcher import WatchedProcess


@pytest.fixture
def games(monkeypatch):
    rule = WatchedProcess(match="helldivers2.exe", label="Helldivers 2", game=True)
    game_mode.set_games([(rule.label, rule.matches)])
    started = []
    monkeypatch.setattr(app_control, "resolve_executable", lambda name: f"C:\\Games\\{name}.exe")
    monkeypatch.setattr(app_control, "_start", lambda name, target: started.append(target) or "started")
    token = turn_in_progress.set(True)
    yield started
    turn_in_progress.reset(token)
    game_mode.drop_after_turn()
    game_mode.set_games([])
    game_mode.active = False


def test_a_game_launch_waits_for_the_turn_to_end(games):
    out = app_control.launch_app("Helldivers 2")
    assert games == [] and "Game mode comes on first" in out


@pytest.mark.asyncio
async def test_after_the_turn_game_mode_comes_on_then_the_game_starts(games, monkeypatch):
    app_control.launch_app("Helldivers 2")
    order = []

    async def release():
        order.append("released")
        return ["llama-server"]

    monkeypatch.setattr(game_mode, "_pause", lambda: None)
    monkeypatch.setattr(game_mode, "_release", release)
    monkeypatch.setattr(game_mode, "_vram_used_mb", lambda: None)
    monkeypatch.setattr(app_control, "_start", lambda name, target: order.append("launched") or "ok")
    await game_mode.run_after_turn()
    assert game_mode.active and order == ["released", "launched"]


def test_a_game_launch_outside_a_chat_turn_starts_at_once(games):
    token = turn_in_progress.set(False)
    try:
        app_control.launch_app("Helldivers 2")
    finally:
        turn_in_progress.reset(token)
    assert len(games) == 1


def test_an_ordinary_app_starts_at_once(games):
    app_control.launch_app("notepad")
    assert len(games) == 1


def test_a_stopped_turn_asks_for_nothing(games):
    app_control.launch_app("Helldivers 2")
    game_mode.drop_after_turn()
    assert game_mode._after_turn == []  # noqa: SLF001


@pytest.mark.asyncio
async def test_the_tool_parks_a_request_and_refuses_outside_a_turn(games):
    assert "turns on as soon as this reply ends" in app_control.enter_game_mode()
    assert len(game_mode._after_turn) == 1  # noqa: SLF001
    token = turn_in_progress.set(False)
    try:
        assert app_control.enter_game_mode().startswith("Error")
    finally:
        turn_in_progress.reset(token)


def test_the_tool_is_registered_with_a_schema():
    assert "enter_game_mode" in TOOL_FUNCTIONS and "enter_game_mode" in TOOL_SCHEMAS
