"""
Forgotten memories, kept restorable for a while instead of deleted outright.

On 2026-09-29 the model misread "i gave the wrong name, it's actually called qwen" as a memory
correction and forgot the user's only saved preference, re-saving and forgetting it twice more
in the same reply. Nothing told the user and nothing could bring it back. So ``forget`` now
moves a memory here: a small JSON file under the repo-root ``data/`` (the restart-surviving
state pattern -- loaded on use, rewritten on every change), from which ``restore`` puts the
exact row back. Entries older than ``KEEP_DAYS`` are dropped whenever the bin is written.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from app.config import BASE_DIR

logger = logging.getLogger("jarvis.memory.bin")

KEEP_DAYS = 30
# Tests point this at a temp file (conftest), like the database.
BIN_PATH = Path(os.environ.get("JARVIS_MEMORY_BIN_PATH") or (BASE_DIR.parent / "data" / "memory_bin.json"))

_lock = threading.Lock()
_FIELDS = ("id", "project_id", "category", "content", "source_session_id", "confidence", "pinned",
           "created_at", "updated_at", "last_used_at")


def _load() -> list[dict[str, Any]]:
    try:
        if BIN_PATH.exists():
            data = json.loads(BIN_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [e for e in data if isinstance(e, dict) and e.get("id")]
    except Exception as e:
        logger.warning("Could not read the memory bin %s: %s", BIN_PATH, e)
    return []


def _save(entries: list[dict[str, Any]]) -> None:
    cutoff = (datetime.utcnow() - timedelta(days=KEEP_DAYS)).isoformat()
    kept = [e for e in entries if str(e.get("forgotten_at", "")) >= cutoff]
    BIN_PATH.parent.mkdir(parents=True, exist_ok=True)
    BIN_PATH.write_text(json.dumps(kept, indent=2), encoding="utf-8")


def _jsonable(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def add(memory: Any, reason: str = "", session_id: Optional[str] = None) -> None:
    """Keep a copy of a Memory row that is about to be deleted."""
    entry = {f: _jsonable(getattr(memory, f, None)) for f in _FIELDS}
    entry.update(forgotten_at=datetime.utcnow().isoformat(), forgotten_in_session=session_id, reason=reason)
    with _lock:
        entries = [e for e in _load() if e.get("id") != entry["id"]]
        entries.append(entry)
        _save(entries)


def list_entries() -> list[dict[str, Any]]:
    """What is in the bin, most recently forgotten first."""
    with _lock:
        return sorted(_load(), key=lambda e: str(e.get("forgotten_at", "")), reverse=True)


def restore(memory_id: str, session_factory) -> Optional[str]:
    """Put a forgotten memory back, exactly as it was. Returns its content, or None if the bin
    has no entry with that id (a unique prefix of it is accepted)."""
    from app.database.models import Memory

    key = (memory_id or "").strip().replace("-", "").lower()
    with _lock:
        entries = _load()
        matches = [e for e in entries if str(e["id"]).replace("-", "").lower().startswith(key)] if len(key) >= 4 else []
        if len(matches) != 1:
            return None
        entry = matches[0]
        row = {f: entry.get(f) for f in _FIELDS}
        for f in ("created_at", "updated_at", "last_used_at"):
            if row.get(f):
                row[f] = datetime.fromisoformat(row[f])
        row["confidence"] = row.get("confidence") if row.get("confidence") is not None else 1.0
        row["pinned"] = bool(row.get("pinned"))
        with session_factory() as db:
            if db.get(Memory, row["id"]) is None:
                db.add(Memory(**row))
                db.commit()
        _save([e for e in entries if e is not entry])
    logger.info("Memory %s restored from the bin", str(entry["id"])[:8])
    return str(entry.get("content") or "")
