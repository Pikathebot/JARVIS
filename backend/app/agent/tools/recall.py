"""``recall``: search earlier conversations (PLAN 4.18 phase 3).

The model only sees the open episode of the Freeform thread (plus a short digest), so anything
older is reached through this tool: "what did we decide about the GPU?", "the command you gave me
yesterday". It searches two things:

* **episode records** of the thread (title, summary, decisions, open items, written by the thread
  keeper) -- by keyword, and by meaning when the embedding sidecar can serve (each record's vector
  is stored with it; the query is embedded here);
* **raw messages** -- keyword search over the FTS5 index (``app.database.message_index``), so exact
  words, commands and numbers are found even when no summary kept them.

Scope, decided with the user: from Freeform it searches the thread and every real project's
chats; from a project chat, only that project's chats -- personal material never surfaces in a
project. The open episode is left out (the model already has it). ``when`` narrows to a time
span ("yesterday", "last week", "monday", "2026-10-18"); with no query it lists what was
discussed then. Off-the-record chats are never stored, so never found.
"""
from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, timedelta
from typing import Any, Callable, Optional

from sqlalchemy import text
from sqlmodel import Session

from app.database.message_index import ensure_message_index
from app.database.session import SessionLocal

THREAD_SESSION_ID = "jarvis-thread"  # app.memory.thread's; not imported (tools load before memory)
DEFAULT_PROJECT_ID = "default-workspace"  # sessions with no chosen project: not a project
MAX_GROUPS = 4
MAX_EXCERPTS = 3
EXCERPT_CHARS = 240
SEMANTIC_FLOOR = 0.55

# Overridable in tests; the app's own database otherwise.
session_factory: Callable[[], Session] = SessionLocal

_STOP = set("a an the and or of to in on at for is was were be been it its this that these those i you we me my "
            "your our what which who whom how when where why did do does about with from as by again earlier "
            "said say told tell talk talked us remember recall".split())
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


# --- when ---------------------------------------------------------------------------------------

def _day_span(d: date, days: int = 1) -> tuple[float, float]:
    start = datetime.combine(d, datetime.min.time())
    return start.timestamp(), (start + timedelta(days=days)).timestamp()


def parse_past(when: str, now: Optional[datetime] = None) -> Optional[tuple[float, float]]:
    """A past time span as (start, end) timestamps, or None if unreadable: 'today', 'yesterday',
    'this/last week', 'this/last month', 'N days ago', 'a few days ago', '(last) monday',
    '2026-10-18', '18 oct' / 'oct 18'."""
    now = now or datetime.now()
    t = " ".join(re.sub(r"\b(?:on|in|the|during|earlier)\b", " ", str(when or "").lower().replace(",", " ")).split())
    # "Sunday 18 Oct" (what Ornith sent on the first live probe): with a date there, the weekday adds nothing.
    dated = re.sub(r"\b(?:" + "|".join(_WEEKDAYS) + r"|" + "|".join(d[:3] for d in _WEEKDAYS) + r")\b", " ", t)
    if re.search(r"\d", dated):
        t = " ".join(dated.split())
    t = re.sub(r"\b(" + "|".join(d[:3] for d in _WEEKDAYS) + r")\b",
               lambda m: _WEEKDAYS[[d[:3] for d in _WEEKDAYS].index(m.group(1))], t)
    today = now.date()
    if t in ("today", "this morning", "tonight", "this evening", "this afternoon"):
        return _day_span(today)
    if t in ("yesterday", "last night"):
        return _day_span(today - timedelta(days=1))
    if t == "this week":
        return _day_span(today - timedelta(days=today.weekday()), today.weekday() + 1)
    if t == "last week":
        return _day_span(today - timedelta(days=today.weekday() + 7), 7)
    if t in ("this month",):
        return _day_span(today.replace(day=1), today.day)
    if t == "last month":
        first = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
        return _day_span(first, (today.replace(day=1) - first).days)
    if t in ("a few days ago", "few days ago", "recently", "the other day"):
        return _day_span(today - timedelta(days=6), 6)
    m = re.fullmatch(r"(\d{1,3}|a|one|two|three) (day|week)s? ago", t)
    if m:
        n = {"a": 1, "one": 1, "two": 2, "three": 3}.get(m.group(1)) or int(m.group(1))
        if m.group(2) == "day":
            return _day_span(today - timedelta(days=n))
        return _day_span(today - timedelta(days=today.weekday() + 7 * n), 7)
    m = re.fullmatch(r"(last )?(" + "|".join(_WEEKDAYS) + r")", t)
    if m:
        back = (today.weekday() - _WEEKDAYS.index(m.group(2))) % 7
        if back == 0 and m.group(1):
            back = 7
        return _day_span(today - timedelta(days=back))
    try:
        return _day_span(date.fromisoformat(t))
    except ValueError:
        pass
    m = re.fullmatch(r"(?:(\d{1,2})(?:st|nd|rd|th)? ([a-z]{3})[a-z]*|([a-z]{3})[a-z]* (\d{1,2})(?:st|nd|rd|th)?)(?: (\d{4}))?", t)
    if m:
        day_s, mon = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
        if mon in _MONTHS:
            try:
                d = date(int(m.group(5) or today.year), _MONTHS.index(mon) + 1, int(day_s))
            except ValueError:
                return None
            if d > today and not m.group(5):
                d = d.replace(year=today.year - 1)
            return _day_span(d)
    return None


# --- vectors ------------------------------------------------------------------------------------

def _sidecar_vectors(texts: list[str], query: bool) -> Optional[list[list[float]]]:
    """Real embeddings from the sidecar, or None -- never the hash fallback, which would compare
    meaninglessly with stored vectors."""
    try:
        from app.config import settings
        from app.rag.embedding_sidecar import get_embedding_sidecar

        if not settings.rag_embedding_sidecar_enabled:
            return None
        prefix = settings.rag_embedding_query_prefix if query else settings.rag_embedding_document_prefix
        sidecar = get_embedding_sidecar()
        return sidecar.embed([prefix + t for t in texts]) if sidecar else None
    except Exception:
        return None


def record_vector(record: dict[str, Any]) -> Optional[str]:
    """The stored vector (JSON) for an episode record; the thread keeper calls it."""
    body = " ".join(str(record.get(k) or "") for k in ("title", "summary", "decisions", "open_items")).strip()
    vectors = _sidecar_vectors([body], query=False) if body else None
    return json.dumps([round(x, 6) for x in vectors[0]]) if vectors else None


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


# --- search -------------------------------------------------------------------------------------

def _terms(query: str) -> list[str]:
    words = [w.strip(".-") for w in re.findall(r"[\w.-]+", query.lower())]
    return [w for w in dict.fromkeys(words) if w and w not in _STOP and (len(w) > 1 or w.isdigit())]


def _clock(ts: Optional[float]) -> str:
    return datetime.fromtimestamp(ts).strftime("%a %d %b %H:%M") if ts else "?"


def _excerpt(content: str, terms: list[str]) -> str:
    flat = " ".join((content or "").split())
    if len(flat) <= EXCERPT_CHARS:
        return flat
    low = flat.lower()
    at = min((low.find(t) for t in terms if low.find(t) >= 0), default=0)
    start = max(0, at - EXCERPT_CHARS // 3)
    piece = flat[start:start + EXCERPT_CHARS]
    return ("..." if start else "") + piece + ("..." if start + EXCERPT_CHARS < len(flat) else "")


def recall(query: str = "", when: str = "", session_id: Optional[str] = None,
           project_id: Optional[str] = None) -> str:
    """Search earlier conversations for what was said, decided or left open, when the user refers to something you don't have in front of you.

    Args:
        query: Words to look for; empty to list what was discussed in ``when``.
        when: A past time span: 'today', 'yesterday', 'last week', 'monday', '2026-10-18'; empty for any time.
    """
    span = None
    if str(when or "").strip():
        span = parse_past(when)
        if span is None:
            return (f"Error: couldn't read '{when}' as a past time. Use 'today', 'yesterday', 'this week', "
                    "'last week', 'monday', '3 days ago' or a date like '2026-10-18'.")
    terms = _terms(query or "")
    if not terms and span is None:
        return "Error: say what to look for (query), or when (e.g. when='yesterday')."

    in_project = bool(project_id) and project_id != DEFAULT_PROJECT_ID
    with session_factory() as db:
        conn = db.connection()
        ensure_message_index(conn)  # a no-op on the app's DB (Alembic 008 made it); tests build from models
        db.commit()
        conn = db.connection()  # the commit released the first one
        if in_project:
            sessions = {r[0]: (r[1], None) for r in conn.execute(
                text("SELECT session_id, title FROM sessions WHERE project_id = :p"), {"p": project_id})}
        else:
            sessions = {r[0]: (r[1], r[2]) for r in conn.execute(text(
                "SELECT s.session_id, s.title, p.name FROM sessions s JOIN projects p ON p.id = s.project_id "
                "WHERE s.project_id IS NOT NULL AND s.project_id != :d"), {"d": DEFAULT_PROJECT_ID})}
            sessions[THREAD_SESSION_ID] = ("this chat", None)
        if not sessions:
            return "Nothing to search yet: no earlier conversations."

        episodes = []
        if THREAD_SESSION_ID in sessions:
            episodes = [dict(r._mapping) for r in conn.execute(text(
                "SELECT * FROM episodes WHERE session_id = :s ORDER BY id"), {"s": THREAD_SESSION_ID})]
        open_ep = next((e for e in episodes if e["record_status"] == "open"), None)
        skip_after = open_ep["start_after_message_id"] if open_ep else None
        closed = [e for e in episodes if e["record_status"] != "open"]

        hits: list[dict[str, Any]] = []
        if terms:
            marks = ", ".join(f":s{i}" for i in range(len(sessions)))
            params: dict[str, Any] = {f"s{i}": s for i, s in enumerate(sessions)}
            params["q"] = " OR ".join('"' + t.replace('"', "") + '"' for t in terms)
            sql = ("SELECT m.id, m.session_id, m.role, m.content, m.created_at, bm25(messages_fts) AS rank "
                   "FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid "
                   f"WHERE messages_fts MATCH :q AND m.role IN ('user', 'assistant') AND m.session_id IN ({marks})")
            if span:
                sql += " AND m.created_at >= :a AND m.created_at < :b"
                params.update(a=span[0], b=span[1])
            sql += " ORDER BY rank LIMIT 60"
            hits = [dict(r._mapping) for r in conn.execute(text(sql), params)]
            hits = [h for h in hits if not (h["session_id"] == THREAD_SESSION_ID and skip_after is not None
                                            and h["id"] > skip_after)]

        def episode_of(message_id: int) -> Optional[dict[str, Any]]:
            owner = None
            for e in episodes:
                if e["start_after_message_id"] < message_id:
                    owner = e
            return owner if owner is not None and owner["record_status"] != "open" else None

        groups: dict[tuple[str, Any], dict[str, Any]] = {}

        def group(key: tuple[str, Any], **info: Any) -> dict[str, Any]:
            return groups.setdefault(key, {"score": 0.0, "hits": [], **info})

        for h in hits:
            if h["session_id"] == THREAD_SESSION_ID:
                ep = episode_of(h["id"])
                g = group(("ep", ep["id"]), episode=ep) if ep else group(("thread", 0), episode=None)
            else:
                g = group(("s", h["session_id"]), session=h["session_id"])
            g["hits"].append(h)
            g["score"] += 1.0

        qvec = None
        if terms and any(e.get("embedding") for e in closed):
            vectors = _sidecar_vectors([query], query=True)
            qvec = vectors[0] if vectors else None
        for ep in closed:
            if span and not (ep["started_at"] < span[1] and (ep.get("ended_at") or ep["started_at"]) >= span[0]):
                continue
            record = " ".join(str(ep.get(k) or "") for k in ("title", "summary", "decisions", "open_items")).lower()
            kw = sum(1 for t in terms if t in record)
            sem = 0.0
            if qvec and ep.get("embedding"):
                sem = _cosine(qvec, json.loads(ep["embedding"]))
            score = 2.0 * kw + (4.0 * sem if sem >= SEMANTIC_FLOOR else 0.0)
            if not terms:
                score = 1.0  # a time span alone: every episode in it
            if score > 0:
                group(("ep", ep["id"]), episode=ep)["score"] += score
        if not terms and span:
            for sid in sessions:
                if sid == THREAD_SESSION_ID:
                    continue
                n = conn.execute(text("SELECT COUNT(*) FROM messages WHERE session_id = :s AND role = 'user' "
                                      "AND created_at >= :a AND created_at < :b"),
                                 {"s": sid, "a": span[0], "b": span[1]}).scalar()
                if n:
                    group(("s", sid), session=sid)["score"] += 1.0

        if not groups:
            where = f" {when.strip()}" if span else ""
            return f"Nothing found{where} for '{query}'." if terms else f"No conversations found{where}."

        def latest(g: dict[str, Any]) -> float:
            ep = g.get("episode")
            return max([h["created_at"] or 0 for h in g["hits"]] + [(ep or {}).get("started_at") or 0])

        ranked = sorted(groups.values(), key=lambda g: (g["score"], latest(g)), reverse=True)[:MAX_GROUPS]
        if not terms:
            ranked.sort(key=latest)
        lines = [f"Found in {len(ranked)} earlier conversation{'s' if len(ranked) > 1 else ''}"
                 f"{' (of ' + str(len(groups)) + ')' if len(groups) > len(ranked) else ''}:"]
        for n, g in enumerate(ranked, 1):
            ep = g.get("episode")
            if ep:
                end = datetime.fromtimestamp(ep["ended_at"]).strftime("%H:%M") if ep.get("ended_at") else "?"
                lines.append(f"{n}. This chat, {_clock(ep['started_at'])}-{end}: {ep.get('title') or 'untitled'}")
                if ep.get("summary"):
                    lines.append(f"   Summary: {ep['summary']}")
                if ep.get("decisions"):
                    lines.append(f"   Decided: {ep['decisions']}")
                if ep.get("open_items"):
                    lines.append(f"   Open: {ep['open_items']}")
            elif g.get("session"):
                title, project = sessions[g["session"]]
                if not title or title.startswith("Session "):  # the auto title ("Session session_")
                    title = f"chat of {_clock(latest(g))[:10]}"
                lines.append(f"{n}. Project chat{' in ' + project if project else ''}: {title}")
            else:
                lines.append(f"{n}. This chat, before episodes were kept:")
            shown: set[str] = set()
            for h in sorted(g["hits"], key=lambda h: h["rank"]):
                piece = _excerpt(h["content"], terms)
                if piece in shown:  # the same question asked again
                    continue
                shown.add(piece)
                who = "User" if h["role"] == "user" else "Jarvis"
                lines.append(f"   [{_clock(h['created_at'])}] {who}: {piece}")
                if len(shown) >= MAX_EXCERPTS:
                    break
        return "\n".join(lines)
