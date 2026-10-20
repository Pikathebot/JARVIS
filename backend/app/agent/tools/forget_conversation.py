"""``forget_conversation``: delete earlier topics of the Freeform thread when the user asks (PLAN 4.18
phase 5, decision 6).

"Forget what we said about the trip" finds the matching closed episodes of the thread -- by their
record (title, summary, decisions, open items), by the words in their messages, and by meaning
when the embedding sidecar serves -- and deletes them for good: messages, record, vector and search
entries, and later digests are rebuilt without them (``app.memory.thread.forget_episodes``). The
approval card names exactly which conversations go (``describe_matches``, called by the permission
gate); nothing is deleted unless the user's own message asks to forget something. The open
episode is never touched (the reply being written belongs to it). Freeform only: a project chat is
deleted whole from the sidebar. Saved memories are the ``forget`` tool's, not this one's.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable, Optional

from sqlalchemy import text
from sqlmodel import Session

from app.agent.tools.memory import asks_to_forget
from app.agent.tools.recall import SEMANTIC_FLOOR, _clock, _cosine, _sidecar_vectors, _terms, parse_past
from app.database.message_index import ensure_message_index
from app.database.session import SessionLocal

THREAD_SESSION_ID = "jarvis-thread"
MAX_MATCHES = 8

# Overridable in tests; the app's own database otherwise.
session_factory: Callable[[], Session] = SessionLocal


def find_conversations(about: str = "", when: str = "") -> list[dict[str, Any]] | str:
    """The closed thread episodes matching ``about`` / ``when``, best first; an error string when
    neither says anything usable."""
    span = None
    if str(when or "").strip():
        span = parse_past(when)
        if span is None:
            return (f"Error: couldn't read '{when}' as a past time. Use 'today', 'yesterday', 'last week', "
                    "'monday' or a date like '2026-10-18'.")
    terms = _terms(about or "")
    if not terms and span is None:
        return "Error: say what the conversation was about, or when it was."

    with session_factory() as db:
        conn = db.connection()
        ensure_message_index(conn)
        db.commit()
        conn = db.connection()
        episodes = [dict(r._mapping) for r in conn.execute(text(
            "SELECT * FROM episodes WHERE session_id = :s ORDER BY start_after_message_id, id"),
            {"s": THREAD_SESSION_ID})]
        hits: list[int] = []
        if terms:
            q = " OR ".join('"' + t.replace('"', "") + '"' for t in terms)
            hits = [r[0] for r in conn.execute(text(
                "SELECT m.id FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid "
                "WHERE messages_fts MATCH :q AND m.session_id = :s AND m.role IN ('user', 'assistant')"),
                {"q": q, "s": THREAD_SESSION_ID})]

    def owner(message_id: int) -> Optional[dict[str, Any]]:
        found = None
        for e in episodes:
            if e["start_after_message_id"] < message_id:
                found = e
        return found

    message_hits: dict[int, int] = {}
    for mid in hits:
        ep = owner(mid)
        if ep is not None:
            message_hits[ep["id"]] = message_hits.get(ep["id"], 0) + 1

    closed = [e for e in episodes if e["record_status"] != "open"]
    qvec = None
    if terms and any(e.get("embedding") for e in closed):
        vectors = _sidecar_vectors([about], query=True)
        qvec = vectors[0] if vectors else None
    scored = []
    for ep in closed:
        if span and not (ep["started_at"] < span[1] and (ep.get("ended_at") or ep["started_at"]) >= span[0]):
            continue
        if not terms:
            scored.append((1.0, ep))  # a time span alone: every topic in it
            continue
        record = " ".join(str(ep.get(k) or "") for k in ("title", "summary", "decisions", "open_items")).lower()
        score = 2.0 * sum(1 for t in terms if t in record) + min(message_hits.get(ep["id"], 0), 3)
        if qvec and ep.get("embedding"):
            sem = _cosine(qvec, json.loads(ep["embedding"]))
            score += 4.0 * sem if sem >= SEMANTIC_FLOOR else 0.0
        if score > 0:
            scored.append((score, ep))
    scored.sort(key=lambda p: (p[0], p[1]["started_at"]), reverse=True)
    return [ep for _, ep in scored[:MAX_MATCHES]]


def _name(ep: dict[str, Any]) -> str:
    return f"\"{ep.get('title') or 'untitled'}\" ({_clock(ep['started_at'])})"


def describe_matches(about: str = "", when: str = "") -> tuple[int, str]:
    """For the approval card: how many conversations would go, and which."""
    found = find_conversations(about, when)
    if isinstance(found, str) or not found:
        return 0, ""
    names = "; ".join(_name(ep) for ep in found)
    return len(found), (f"Deletes {len(found)} earlier conversation{'s' if len(found) > 1 else ''} for good -- "
                        f"messages and summaries; recall won't find {'them' if len(found) > 1 else 'it'} again: {names}.")


def forget_conversation(about: str = "", when: str = "", session_id: Optional[str] = None,
                        user_message: Optional[str] = None) -> str:
    """Delete earlier topics of this chat for good, only when the user asks to forget what was said about something (saved memories: forget).

    Args:
        about: What the conversation was about, in the user's words.
        when: A past time span: 'yesterday', 'last week', 'monday', '2026-10-18'; empty for any time.
    """
    if session_id != THREAD_SESSION_ID:
        return ("Error: only the main Freeform conversation's topics can be forgotten this way; "
                "a side or project chat is deleted from the sidebar.")
    if not asks_to_forget(user_message or ""):
        return "Error: the user didn't ask to forget anything. Never delete conversations unasked."
    found = find_conversations(about, when)
    if isinstance(found, str):
        return found
    if not found:
        return f"Nothing earlier matches '{about or when}'; nothing was deleted. (The current topic can't be forgotten while it is open.)"

    from app.memory.store import MemoryStore
    from app.memory.thread import forget_episodes

    result = forget_episodes(MemoryStore(session_factory=session_factory), [ep["id"] for ep in found])
    return (f"Forgotten: {'; '.join(_name(ep) for ep in found)} -- {result['messages']} message(s) deleted; "
            "recall can't find them any more. The current topic was not touched.")
