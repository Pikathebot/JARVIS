"""look_at_screen (tool review batch 5, 2026-10-01): capture, downscale, hand the image to the
model on the tool result when it can see, say so when it can't; runs only when asked to look."""
from unittest.mock import patch

from PIL import Image

from app.agent.permissions import evaluate_tool_permission
from app.agent.tools import screen


def _fake_grab(*args, **kwargs):
    return Image.new("RGB", (2560, 1440), "white")


def test_capture_is_downscaled_and_saved(tmp_path, monkeypatch):
    monkeypatch.setattr(screen, "SCREENSHOT_DIR", tmp_path)
    with patch("PIL.ImageGrab.grab", _fake_grab):
        out = screen.look_at_screen()
    assert out.startswith("Screenshot of the main screen (1280x720)")
    path = screen.MARKER_RE.search(out).group(1)
    assert Image.open(path).size == (1280, 720)


def test_a_window_that_is_not_open_is_reported(monkeypatch):
    monkeypatch.setattr(screen, "_window_bbox", lambda q: (None, ""))
    assert screen.look_at_screen(window="photoshop") == "No open window matches 'photoshop'."
    monkeypatch.setattr(screen, "_window_bbox", lambda q: (None, "Notes - Notepad"))
    assert "minimized" in screen.look_at_screen(window="notepad")


def test_the_model_gets_pixels_only_when_it_can_see(tmp_path):
    shot = tmp_path / "s.png"
    Image.new("RGB", (10, 10)).save(shot)
    result = f"Screenshot of the main screen (10x10), taken 12:00:00. [screenshot:{shot}]"
    parts = screen.screenshot_tool_content(result, can_see=True)
    assert parts[0] == {"type": "text", "text": "Screenshot of the main screen (10x10), taken 12:00:00. [screenshot]"}
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
    blind = screen.screenshot_tool_content(result, can_see=False)
    assert isinstance(blind, str) and "can't see images right now" in blind
    assert screen.screenshot_tool_content("Error: x", can_see=True) == "Error: x"


def test_it_runs_when_asked_to_look_and_asks_otherwise():
    for message in ("what's on my screen?", "look at this error", "can you see this window",
                    "take a screenshot"):
        assert evaluate_tool_permission("look_at_screen", {}, user_message=message).allowed, message
    unprompted = evaluate_tool_permission("look_at_screen", {}, user_message="open discord")
    assert not unprompted.allowed and "didn't ask" in unprompted.reason
