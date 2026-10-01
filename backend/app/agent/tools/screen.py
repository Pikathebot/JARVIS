"""
look_at_screen: a screenshot of the screen or one window, handed to the model as an image for
that turn (tool review, 2026-09-30). Jarvis's own windows are excluded from capture by the app
(WDA_EXCLUDEFROMCAPTURE), so they never show up in the shot.

The tool returns text with a ``[screenshot:<path>]`` marker; the orchestrator turns that into an
image part on the tool result when the model can see (measured 2026-10-01: llama-server and the
Qwen3.5 template accept an image inside a tool result and the 9B read it correctly), or tells
the model it can't see right now. History keeps the text, so later turns don't resend pixels.
"""
import logging
import os
import re
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.config import BASE_DIR

logger = logging.getLogger("jarvis.agent.tools.screen")

MAX_SIDE = 1280
SCREENSHOT_DIR = Path(os.environ.get("JARVIS_SCREENSHOT_DIR") or (BASE_DIR / "data" / "screenshots"))
MARKER_RE = re.compile(r"\[screenshot:([^\]]+)\]")


def _window_bbox(query: str) -> tuple[Optional[tuple[int, int, int, int]], str]:
    """Screen rectangle and title of the best-matching visible window."""
    import ctypes
    from ctypes import wintypes
    from app.agent.tools.app_control import _visible_windows

    q = query.strip().lower()
    q_exe = q[:-4] if q.endswith(".exe") else q
    best = None
    for hwnd, title, pname in _visible_windows():
        stem = pname.lower()[:-4] if pname.lower().endswith(".exe") else pname.lower()
        rank = 0 if stem == q_exe else 1 if title.lower() == q else 2 if q in title.lower() else 3 if q_exe in stem else None
        if rank is not None and (best is None or rank < best[0]):
            best = (rank, hwnd, title)
    if best is None:
        return None, ""
    _, hwnd, title = best
    user32 = ctypes.windll.user32
    if user32.IsIconic(hwnd):
        return None, title
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return (rect.left, rect.top, rect.right, rect.bottom), title


def look_at_screen(window: str = "", session_id: Optional[str] = None) -> str:
    """
    Take a screenshot of the main screen, or of one open window (by app or title), and look at it -- when the user asks what is on their screen or about something they are looking at.

    Args:
        window: Optional app name or window title to capture just that window.
    """
    try:
        from PIL import ImageGrab
    except ImportError:
        return "Error: screen capture is unavailable (Pillow is not installed)."

    what = "the main screen"
    try:
        if window and window.strip():
            bbox, title = _window_bbox(window)
            if bbox is None:
                return (f"'{title}' is minimized, so there is nothing to see; ask the user to open it." if title
                        else f"No open window matches '{window}'.")
            image = ImageGrab.grab(bbox=bbox, all_screens=True)
            what = f"the window '{title}'"
        else:
            image = ImageGrab.grab(all_screens=False)
    except Exception as e:
        logger.warning("Screen capture failed: %s", e)
        return f"Error: couldn't capture the screen ({e})."

    w, h = image.size
    scale = min(1.0, MAX_SIDE / max(w, h))
    if scale < 1.0:
        image = image.resize((max(1, int(w * scale)), max(1, int(h * scale))))

    from app.agent.tools.memory import _is_ephemeral
    folder = Path(tempfile.gettempdir()) / "jarvis-ephemeral" if _is_ephemeral(session_id) else SCREENSHOT_DIR
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"screen-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}.png"
    image.convert("RGB").save(path, "PNG")
    logger.info("Screenshot of %s saved to %s (%dx%d)", what, path, *image.size)
    return f"Screenshot of {what} ({image.size[0]}x{image.size[1]}), taken {datetime.now():%H:%M:%S}. [screenshot:{path}]"


def screenshot_tool_content(result: str, can_see: bool):
    """The tool message content for a look_at_screen result: text plus the image when the model
    can see, else text that says it can't (no projector loaded -- usually low VRAM)."""
    m = MARKER_RE.search(result or "")
    if not m:
        return result
    text = MARKER_RE.sub("[screenshot]", result).strip()
    path = Path(m.group(1))
    if not can_see:
        return (text + " You can't see images right now: the vision part of the model isn't loaded "
                "(usually low VRAM). Tell the user that, instead of describing the screen.")
    try:
        import base64
        uri = "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()
    except OSError as e:
        return f"{text} (The screenshot file couldn't be read: {e}.)"
    return [{"type": "text", "text": text}, {"type": "image_url", "image_url": {"url": uri}}]
