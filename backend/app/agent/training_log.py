"""
The training log for the Ornith fine-tune (PLAN §4 step 1, 4.13 #10): what the safety nets caught
and what the user flagged, appended to ``data/training_log.jsonl`` at the repo root (git-ignored,
local only; personal details are stripped before anything is uploaded, not here).

Three kinds of record, one JSON object per line:

- ``net``: a turn where a net fired (claim check, nudge, thinking loop, ...). It carries the user's
  message, each draft a net sent back with the note that sent it, the tools that ran and the reply
  that was kept -- the "what the model did / what the net fixed" pair.
- ``capture``: the memory or PROJECT.md net saved something the reply itself should have.
- ``wrong``: the user tapped "got it wrong" on a reply, with what they meant if they said. A later
  ``wrong`` for the same ``turn_id`` supersedes an earlier one; ``unflagged`` withdraws it.

Every finished turn is kept in memory for a while (``turn_id`` on the ``done`` event) so a flag can
find it; that, too, is skipped for off-the-record turns, which never reach this file.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("jarvis.agent.training_log")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
LOG_PATH = Path(os.environ.get("JARVIS_TRAINING_LOG_PATH") or REPO_ROOT / "data" / "training_log.jsonl")
KEEP_TURNS = 200  # how many recent turns a flag can still reach
# Nets about the harness, not the model: fitting a long chat into the window isn't a model mistake.
_NOT_MODEL_NETS = frozenset({"fit-window"})
_ARG_CHARS = 500
_RESULT_CHARS = 1000
_DRAFT_CHARS = 4000

_lock = threading.Lock()
_turns: "OrderedDict[str, dict[str, Any]]" = OrderedDict()


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _clip(value: Any, limit: int) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"... [{len(value) - limit} more chars]"
    return value


def _tools_summary(tools_used: list[Any]) -> list[dict[str, Any]]:
    out = []
    for item in tools_used or []:
        if not isinstance(item, dict):
            continue
        out.append({
            "tool": item.get("tool"),
            "args": {k: _clip(v, _ARG_CHARS) for k, v in (item.get("args") or {}).items()},
            "status": item.get("status"),
            "result": _clip(item.get("result"), _RESULT_CHARS),
        })
    return out


def _append(record: dict[str, Any], path: Optional[Path] = None) -> None:
    target = path or LOG_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with _lock, target.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError as e:  # the log must never break a turn
        logger.warning("Training log write to %s failed: %s", target, e)


def close_turn(
    *,
    session_id: str,
    model: str,
    user_message: str,
    reply: str,
    tools_used: list[Any],
    nets: list[str],
    sent_back: list[dict[str, Any]],
    ephemeral: bool,
    path: Optional[Path] = None,
) -> Optional[str]:
    """Keep a finished turn for a flag and log it if a net fired. Returns its ``turn_id`` (None off the record)."""
    if ephemeral:
        return None
    turn_id = uuid.uuid4().hex[:12]
    turn = {
        "turn_id": turn_id,
        "session_id": session_id,
        "model": model,
        "user_message": user_message,
        "reply": reply,
        "tools": _tools_summary(tools_used),
        "nets": [n for n in nets if n not in _NOT_MODEL_NETS],
        # a looping thought can run to the reply cap: the start is what shows the mistake
        "sent_back": [{**item, "draft": _clip(item.get("draft"), _DRAFT_CHARS)} for item in sent_back],
    }
    with _lock:
        _turns[turn_id] = turn
        while len(_turns) > KEEP_TURNS:
            _turns.popitem(last=False)
    if turn["nets"]:
        _append({"kind": "net", "time": _now(), **turn}, path)
    return turn_id


def record_capture(
    kind: str,
    *,
    session_id: Optional[str],
    model: str,
    user_message: str,
    saved: Any,
    ephemeral: bool,
    path: Optional[Path] = None,
) -> None:
    """The memory / PROJECT.md net saved something after the reply (``kind`` = memory / project)."""
    if ephemeral or not saved:
        return
    _append({"kind": "capture", "net": kind, "time": _now(), "session_id": session_id, "model": model,
             "user_message": user_message, "saved": _clip(saved if isinstance(saved, str) else json.dumps(
                 saved, ensure_ascii=False, default=str), _RESULT_CHARS)}, path)


def flag_wrong(turn_id: str, meant: str = "", path: Optional[Path] = None) -> bool:
    """The user's "got it wrong" on a reply. False when the turn is unknown (too old, or before a restart)."""
    with _lock:
        turn = _turns.get(turn_id)
    if turn is None:
        return False
    _append({"kind": "wrong", "time": _now(), **turn, "meant": (meant or "").strip()}, path)
    return True


def unflag(turn_id: str, path: Optional[Path] = None) -> bool:
    with _lock:
        known = turn_id in _turns
    if known:
        _append({"kind": "unflagged", "time": _now(), "turn_id": turn_id}, path)
    return known
