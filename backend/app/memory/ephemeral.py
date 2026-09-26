"""
Ephemeral (scratchpad) conversations: a turn runs through the normal orchestrator, but the
conversation lives only in this process's memory and nothing about it reaches SQLite -- no
session row, no messages, no compaction events, no tool-call audits, and so nothing in the
session list either.

``EphemeralMemoryStore`` stands in for ``MemoryStore`` for one request (the chat endpoints build
a fresh orchestrator per request, so the swap is local to the turn). It keeps the read side of
the database -- the orchestrator looks up the active project and session attachments through
``_get_session`` / ``_session_factory`` -- but every write that would leave a trace of the
conversation lands in ``EphemeralRegistry`` instead. Purging a session (the client toggling
ephemeral off, or resetting it) drops it immediately; a session nobody touches for
``IDLE_TTL_SECONDS`` is dropped on the next access, so a client that died mid-conversation does
not leave it in memory for the life of the backend.

The long-term context an ephemeral chat *should* keep -- persona and the user's profile
memories -- is read, never written: see ``build_profile_context``.
"""

from __future__ import annotations

import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from sqlmodel import Session, col, select

IDLE_TTL_SECONDS = 6 * 60 * 60
_SAFE_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")

# Memory categories that describe the user rather than a project or a piece of work.
PROFILE_CATEGORIES = ("user_preference", "recurring_fact", "workflow_preference")
PROFILE_MAX_ITEMS = 12


@dataclass
class _EphemeralSession:
    chat_mode: str
    project_id: Optional[str]
    created_at: float
    updated_at: float
    messages: list[dict[str, Any]] = field(default_factory=list)
    compactions: list[dict[str, Any]] = field(default_factory=list)
    upload_dir: Optional[Path] = None


class EphemeralRegistry:
    """Process-wide holder of every live ephemeral conversation, keyed by session id."""

    def __init__(self, idle_ttl_seconds: float = IDLE_TTL_SECONDS):
        self._lock = threading.Lock()
        self._sessions: dict[str, _EphemeralSession] = {}
        self._idle_ttl = idle_ttl_seconds

    def _expire_idle_locked(self, now: float) -> list[_EphemeralSession]:
        stale = [sid for sid, s in self._sessions.items() if now - s.updated_at >= self._idle_ttl]
        return [self._sessions.pop(sid) for sid in stale]

    def get_or_create(self, session_id: str, chat_mode: Optional[str], project_id: Optional[str]) -> _EphemeralSession:
        now = time.time()
        with self._lock:
            expired = self._expire_idle_locked(now)
            sess = self._sessions.get(session_id)
            if sess is None:
                sess = _EphemeralSession(
                    chat_mode=(chat_mode or "WORKSPACE").upper(),
                    project_id=project_id,
                    created_at=now,
                    updated_at=now,
                )
                self._sessions[session_id] = sess
            else:
                if chat_mode:
                    sess.chat_mode = chat_mode.upper()
                if project_id is not None:
                    sess.project_id = project_id
        for old in expired:
            _remove_upload_dir(old)
        return sess

    def get(self, session_id: str) -> Optional[_EphemeralSession]:
        with self._lock:
            return self._sessions.get(session_id)

    def contains(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._sessions

    def purge(self, session_id: str) -> bool:
        """Drops the conversation and any files uploaded into it. True if it existed."""
        with self._lock:
            sess = self._sessions.pop(session_id, None)
        if sess is None:
            return False
        _remove_upload_dir(sess)
        return True

    def upload_dir(self, session_id: str, base: Path) -> Path:
        """A per-session directory for attachments, deleted with the session. The id becomes a
        directory name, so only plain ids are accepted (no separators, no dots)."""
        if not _SAFE_SESSION_ID.fullmatch(session_id or ""):
            raise ValueError(f"unsafe ephemeral session id: {session_id!r}")
        sess = self.get_or_create(session_id, chat_mode=None, project_id=None)
        with self._lock:
            if sess.upload_dir is None:
                sess.upload_dir = (base / session_id).resolve()
            target = sess.upload_dir
        target.mkdir(parents=True, exist_ok=True)
        return target

    def clear(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for sess in sessions:
            _remove_upload_dir(sess)


def _remove_upload_dir(sess: _EphemeralSession) -> None:
    if sess.upload_dir is not None:
        shutil.rmtree(sess.upload_dir, ignore_errors=True)


ephemeral_registry = EphemeralRegistry()


class EphemeralMemoryStore:
    """
    The subset of ``MemoryStore`` the orchestrator and ``ContextManager`` use during a turn,
    backed by ``EphemeralRegistry``. Reads that are not about the conversation (the active
    project, attachment rows) go to the real database through ``backing``; nothing is written.
    Only ``get_or_create_session`` (turn setup) creates a conversation: the other writes are
    dropped once it has been purged, so a reply that finishes after the user reset the
    scratchpad does not bring it back.
    """

    def __init__(self, backing: Any, registry: Optional[EphemeralRegistry] = None):
        self._backing = backing
        self._registry = registry or ephemeral_registry
        self._session_factory: Callable[[], Session] = backing._session_factory

    def _get_session(self) -> Session:
        return self._backing._get_session()

    # ---- sessions ------------------------------------------------------------------------

    def get_or_create_session(
        self,
        session_id: str,
        title: Optional[str] = None,
        chat_mode: Optional[str] = "WORKSPACE",
        project_id: Optional[str] = None,
    ) -> dict[str, Any]:
        sess = self._registry.get_or_create(session_id, chat_mode, project_id)
        return {
            "session_id": session_id,
            "project_id": sess.project_id,
            "title": title,
            "chat_mode": sess.chat_mode,
            "created_at": sess.created_at,
            "updated_at": sess.updated_at,
            "ephemeral": True,
        }

    def set_session_chat_mode(self, session_id: str, chat_mode: str) -> None:
        self._registry.get_or_create(session_id, chat_mode, None)

    def list_sessions(self, project_id: Optional[str] = None, limit: Optional[int] = None, chat_mode: Optional[str] = None) -> list[dict[str, Any]]:
        return []

    def delete_session(self, session_id: str) -> bool:
        return self._registry.purge(session_id)

    # ---- messages ------------------------------------------------------------------------

    def get_messages(self, session_id: str, limit: Optional[int] = None) -> list[dict[str, Any]]:
        sess = self._registry.get(session_id)
        if sess is None:
            return []
        messages = [dict(m) for m in sess.messages]
        return messages[-limit:] if limit is not None and limit > 0 else messages

    def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        name: Optional[str] = None,
        tool_calls: Optional[list] = None,
        is_summary: bool = False,
        reasoning_content: Optional[str] = None,
    ) -> None:
        sess = self._registry.get(session_id)
        if sess is None:
            return  # purged mid-turn: a late reply must not bring the conversation back
        msg: dict[str, Any] = {"role": role, "content": content or ""}
        if reasoning_content:
            msg["reasoning_content"] = reasoning_content
        if name:
            msg["name"] = name
        if tool_calls:
            msg["tool_calls"] = tool_calls
        if is_summary:
            msg["is_summary"] = True
        sess.messages.append(msg)
        sess.updated_at = time.time()

    def replace_messages(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        sess = self._registry.get(session_id)
        if sess is None:
            return
        sess.messages = [dict(m) for m in messages]
        sess.updated_at = time.time()

    # ---- compaction and audit ------------------------------------------------------------

    def record_compaction(
        self,
        session_id: str,
        strategy: str,
        tokens_before: int,
        tokens_after: int,
        details: Optional[str] = None,
    ) -> dict[str, Any]:
        sess = self._registry.get(session_id)
        if sess is None:
            return {}
        event = {
            "id": len(sess.compactions) + 1,
            "session_id": session_id,
            "strategy": strategy,
            "tokens_before": tokens_before,
            "tokens_after": tokens_after,
            "details": details,
            "created_at": time.time(),
        }
        sess.compactions.append(event)
        return {**event, "timestamp": event["created_at"]}

    def get_compaction_events(self, session_id: str) -> list[dict[str, Any]]:
        sess = self._registry.get(session_id)
        return [dict(e) for e in sess.compactions] if sess else []

    def record_tool_call_audit(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Audit rows are turn artifacts too; an ephemeral turn leaves none."""
        return {}

    def get_tool_call_audits(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    def record_reliability_event(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {}

    def __getattr__(self, name: str) -> Any:
        # Anything not listed above is a MemoryStore method this store has not vetted; failing
        # loudly beats silently writing an ephemeral turn to the database through the backing.
        raise AttributeError(f"EphemeralMemoryStore does not support '{name}'")


def build_profile_context(session_factory: Callable[[], Session], ephemeral: bool = False) -> str:
    """
    The per-turn profile block, on every turn in either space: what Jarvis has learnt about the
    user (global, not project-bound memories in the profile categories, pinned first) -- the
    persona already rides in the system prompt. An ephemeral turn also gets a line telling the
    model the conversation is not kept. Per-turn rather than in the system prompt because the
    memories change, and the prompt prefix must not (see ``build_system_prompt``). Read-only.
    """
    from app.database.models import Memory

    try:
        with session_factory() as db:
            rows = db.exec(
                select(Memory)
                .where(col(Memory.project_id).is_(None))
                .where(col(Memory.category).in_(PROFILE_CATEGORIES))
                .order_by(col(Memory.pinned).desc(), col(Memory.confidence).desc(), col(Memory.updated_at).desc())
                .limit(PROFILE_MAX_ITEMS)
            ).all()
            facts = [(r.id, r.content.strip()) for r in rows if r.content and r.content.strip()]
    except Exception:
        facts = []
    sections = []
    if facts:
        lines = "\n".join(f"- [{_short(mid)}] {fact}" for mid, fact in facts)
        sections.append(f"WHAT YOU KNOW ABOUT THE USER (long-term; use it naturally, do not recite it):\n{lines}")
    if ephemeral:
        sections.append(
            "This is an off-the-record scratchpad conversation: it is not saved, and nothing said "
            "here will be remembered afterwards."
        )
    return "\n\n".join(sections)


PROJECT_MEMORY_MAX_ITEMS = 25


def _short(memory_id: str) -> str:
    from app.agent.tools.memory import short_id
    return short_id(memory_id)


def build_project_memory_context(session_factory: Callable[[], Session], project_id: Optional[str]) -> str:
    """
    The per-turn block of what Jarvis has kept about this project -- conventions, decisions, open
    tasks -- written by its own ``remember`` calls in earlier conversations. Each line carries the
    memory's short id so it can be replaced or forgotten rather than duplicated. Per-turn, like
    the profile, because it changes. Read-only; empty without a project or memories.
    """
    if not project_id:
        return ""
    from app.database.models import Memory

    try:
        with session_factory() as db:
            rows = db.exec(
                select(Memory)
                .where(col(Memory.project_id) == project_id)
                .order_by(col(Memory.pinned).desc(), col(Memory.updated_at).desc())
                .limit(PROJECT_MEMORY_MAX_ITEMS)
            ).all()
            items = [(r.id, r.category, r.content.strip()) for r in rows if r.content and r.content.strip()]
    except Exception:
        return ""
    if not items:
        return ""
    labels = {"important_decision": "decision", "persistent_task_context": "open task"}
    lines = "\n".join(
        f"- [{_short(mid)}] " + (f"({labels[cat]}) " if cat in labels else "") + text
        for mid, cat, text in items
    )
    return (
        "WHAT YOU REMEMBER ABOUT THIS PROJECT (from earlier conversations; replace or forget "
        f"entries that are outdated):\n{lines}"
    )
