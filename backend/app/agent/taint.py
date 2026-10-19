"""
Prompt-injection guard: a turn that has read outside text can't act freely (PLAN 4.13 #6).

The 9B can't reliably tell data from instructions. A fetched page with hidden text ("press Win+R and
type ..."), a screenshot, the clipboard or an uploaded file can all carry instructions, and the tools
that normally run without asking -- typing, key presses, file writes, the clipboard, long-term
memory -- would carry them out. So once a turn has brought outside text in, those tools need the
user's approval for the rest of the turn. Ordinary workspace reads don't taint (read-then-edit is the
common flow); files known to come from outside do: uploads, and files Jarvis wrote while tainted.

Voice mode's direct plans are built by code from the utterance, not chosen by the model, and call
the permission gate without a taint, so they are unaffected.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Optional

# tool -> how the approval card names the source
SOURCES: dict[str, str] = {
    "fetch_url": "a web page",
    "web_search": "web search results",
    "research": "web pages",
    "look_at_screen": "the screen",
    "get_clipboard": "the clipboard",
}
FILE_READS = frozenset({"read_file", "read_artifact"})
SINKS = frozenset({"type_text", "press_keys", "write_file", "patch_file", "set_clipboard", "remember"})
FILE_WRITES = frozenset({"write_file", "patch_file"})
# Tools that both read and write: only their writing actions are sinks (PLAN 4.16).
ACTION_SINKS: dict[str, frozenset[str]] = {
    "notes": frozenset({"add", "append"}),
    "tasks": frozenset({"add", "done", "reopen", "edit", "remove"}),
    "calendar": frozenset({"add", "move", "delete"}),
}


def is_sink(tool: str, args: dict[str, Any]) -> bool:
    if tool in ACTION_SINKS:
        return str((args or {}).get("action") or "list").strip().lower() in ACTION_SINKS[tool]
    return tool in SINKS

_lock = threading.Lock()
_outside_files: dict[str, set[str]] = {}  # session -> resolved paths Jarvis wrote while tainted
_parked: dict[str, tuple[str, float]] = {}  # session -> (source, expiry) across an approval pause
PARK_SECONDS = 120.0  # a little over the 90 s approval window


def _resolve(path: str, workspace_path: Optional[str]) -> Optional[Path]:
    if not path:
        return None
    p = Path(str(path))
    try:
        return (p if p.is_absolute() else Path(workspace_path or ".") / p).resolve()
    except (OSError, ValueError):
        return None


def _is_upload(resolved: Path, workspace_path: Optional[str], project_id: Optional[str]) -> bool:
    if not project_id:
        return False
    try:
        from app.workspaces import attachments_dir

        root = attachments_dir(workspace_path, project_id).resolve()
    except Exception:
        return False
    return resolved == root or root in resolved.parents


def source_of(tool: str, args: dict[str, Any], ctx: dict[str, Any]) -> Optional[str]:
    """What outside text this executed call brought in, or None."""
    if tool in SOURCES:
        return SOURCES[tool]
    if tool in FILE_READS:
        resolved = _resolve(str(args.get("file_path") or args.get("name") or ""), ctx.get("workspace_path"))
        if resolved is None:
            return None
        if str(ctx.get("chat_mode") or "").upper() == "FREEFORM" and tool == "read_file":
            # The user's own folders hold downloads and mail attachments: anything could be in there.
            return "a file from your folders"
        if _is_upload(resolved, ctx.get("workspace_path"), ctx.get("project_id")):
            return "an uploaded file"
        with _lock:
            if str(resolved) in _outside_files.get(str(ctx.get("session_id") or ""), set()):
                return "a file written from outside content"
    return None


def note_write(tool: str, args: dict[str, Any], ctx: dict[str, Any], tainted_by: Optional[str]) -> None:
    """A file written during a tainted turn carries the outside text with it."""
    if not tainted_by or tool not in FILE_WRITES:
        return
    resolved = _resolve(str(args.get("file_path") or ""), ctx.get("workspace_path"))
    if resolved is not None:
        with _lock:
            _outside_files.setdefault(str(ctx.get("session_id") or ""), set()).add(str(resolved))


def park(session_id: str, tainted_by: Optional[str]) -> None:
    """A tainted turn paused for approval: the approval arrives as a new request (the client
    re-sends the message with the approved ids), and that continuation must stay tainted."""
    if tainted_by:
        with _lock:
            _parked[session_id] = (tainted_by, time.monotonic() + PARK_SECONDS)


def resume(session_id: str) -> Optional[str]:
    """The parked taint for an approval continuation, consumed once; None when absent or stale."""
    with _lock:
        item = _parked.pop(session_id, None)
    if item and item[1] > time.monotonic():
        return item[0]
    return None


def forget(session_id: str) -> None:
    with _lock:
        _outside_files.pop(session_id, None)
        _parked.pop(session_id, None)
