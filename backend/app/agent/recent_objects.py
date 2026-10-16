"""
What this session has actually done and looked at, built from tool results -- never from the model's
own words (PLAN 4.13 #4).

The 9B loses track of the object behind a follow-up ("rewrite it", "you didn't update it", "did you
look into the file or just make it up?") and of whether an action really succeeded. Each turn's
context now carries a few lines like

    RECENT ACTIONS (from real tool results, newest first):
    - this turn: read_file story/story_bible.md (ok)
    - 1 turn ago: write_file secret.txt (ok)
    - 2 turns ago: web_search "bleach new arc"; fetch_url https://... (ok)

so "it" resolves against facts and "did you do it?" has a true answer. The block rides on the user
turn (``build_turn_context``), never the cached prefix. State lives in process memory, keyed by
session: it is a short-range record, rebuilt as the conversation goes on, and dropped with the
session (``forget``), which also covers ephemeral sessions.
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from typing import Any

MAX_ITEMS = 6          # lines in the block
MAX_TURNS_BACK = 6     # older entries stop being "recent"
_OBJECT_ARGS = ("file_path", "name_or_path", "name_or_title_substring", "pid_or_name", "url", "query",
                "pattern", "directory_path", "name", "message", "text", "action", "branch", "content")


@dataclass
class Entry:
    turn: int
    tool: str
    obj: str
    ok: bool


@dataclass
class _Session:
    turn: int = 0
    entries: deque = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.entries = deque(maxlen=32)


_lock = threading.Lock()
_sessions: dict[str, _Session] = {}


def _object_of(tool: str, args: dict[str, Any]) -> str:
    if tool == "set_volume":
        level, change = args.get("level"), args.get("change")
        return f"level {level}" if level is not None else f"change {change}"
    if tool == "set_clipboard":
        return "clipboard"
    for key in _OBJECT_ARGS:
        value = args.get(key)
        if value not in (None, ""):
            text = str(value).replace("\n", " ").strip()
            if key == "query":
                text = f'"{text}"'
            return text if len(text) <= 70 else text[:67] + "..."
    return ""


def _succeeded(item: dict[str, Any]) -> bool:
    status = item.get("status")
    if status is not None:
        return status == "success"
    if item.get("timed_out"):
        return False
    return not str(item.get("result") or "").lstrip().startswith("Error")


def begin_turn(session_id: str) -> None:
    """Called once per user turn, before the turn context is built."""
    with _lock:
        _sessions.setdefault(session_id, _Session()).turn += 1


def record(session_id: str, item: dict[str, Any]) -> None:
    """One executed tool call, as the loops put it in ``tools_used`` ({tool, args, status|result})."""
    tool = str(item.get("tool") or item.get("name") or "")
    if not tool:
        return
    args = item.get("args") if isinstance(item.get("args"), dict) else {}
    with _lock:
        s = _sessions.setdefault(session_id, _Session())
        s.entries.append(Entry(s.turn, tool, _object_of(tool, args), _succeeded(item)))


def describe(session_id: str) -> str:
    """The block for the turn context, or "" when nothing recent happened."""
    with _lock:
        s = _sessions.get(session_id)
        if s is None:
            return ""
        recent = [e for e in s.entries if s.turn - e.turn < MAX_TURNS_BACK][-MAX_ITEMS:]
        current = s.turn
    if not recent:
        return ""
    lines = ["RECENT ACTIONS (from real tool results, newest first; 'failed' means it did not happen):"]
    for e in reversed(recent):
        ago = current - e.turn
        when = "this turn" if ago == 0 else ("1 turn ago" if ago == 1 else f"{ago} turns ago")
        obj = f" {e.obj}" if e.obj else ""
        lines.append(f"- {when}: {e.tool}{obj} ({'ok' if e.ok else 'failed'})")
    return "\n".join(lines)


def forget(session_id: str) -> None:
    with _lock:
        _sessions.pop(session_id, None)
