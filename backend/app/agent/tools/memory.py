"""
Long-term memory the model keeps for itself: ``remember`` saves (or replaces) a fact for later
conversations, ``forget`` drops one. What is saved comes back on every turn -- the user's own
facts through ``build_profile_context``, a project's through ``build_project_memory_context``
(both in ``app.memory.ephemeral``) -- each line tagged with a short id so the model can replace
or forget it instead of piling up near-duplicates.

Facts about the user are global (they follow the user into every project and Freeform); project
facts, decisions and open tasks belong to the project the conversation is in. An ephemeral
(off-the-record) conversation never saves anything.
"""
import logging
import re
import uuid
from datetime import datetime
from typing import Callable, Optional

from sqlmodel import Session, col, select

from app.database.models import Memory
from app.database.session import SessionLocal

logger = logging.getLogger("jarvis.agent.tools.memory")

# kind -> (stored category, belongs to the project?)
MEMORY_KINDS: dict[str, tuple[str, bool]] = {
    "about_user": ("recurring_fact", False),
    "preference": ("user_preference", False),
    "workflow": ("workflow_preference", False),
    "project": ("project_preference", True),
    "decision": ("important_decision", True),
    "task": ("persistent_task_context", True),
}
MEMORY_MAX_CHARS = 300
MEMORY_LIMIT_PER_SCOPE = 50
MEMORY_ID_CHARS = 8

# Overridable in tests; the app's own database otherwise.
session_factory: Callable[[], Session] = SessionLocal


def short_id(memory_id: str) -> str:
    return memory_id.replace("-", "")[:MEMORY_ID_CHARS]


def _is_ephemeral(session_id: Optional[str]) -> bool:
    if not session_id:
        return False
    from app.memory.ephemeral import ephemeral_registry
    return ephemeral_registry.get(session_id) is not None


def _scope_filter(stmt, project_id: Optional[str]):
    """The memories a conversation may see and change: the user's global ones, plus its
    project's when it has one."""
    if project_id:
        return stmt.where((col(Memory.project_id) == project_id) | (col(Memory.project_id).is_(None)))
    return stmt.where(col(Memory.project_id).is_(None))


# Words that don't tell two memories apart ("User prefers X" / "User wants X").
_FILLER = frozenset(
    "a an the user user's they them their he she his her i me my is are was were be been to of in on at "
    "for and or with all every any this that these those it its as by from prefers prefer wants want likes "
    "like should always".split()
)


def _content_words(text: str) -> frozenset[str]:
    return frozenset(w for w in re.findall(r"[a-z0-9']+", text.lower()) if w not in _FILLER)


def _same_fact(a: str, b: str) -> bool:
    """The same fact reworded: identical ignoring case/punctuation/filler, or content words
    overlapping >= 75% ("…written in Python" / "…written in Python." / "User wants all code
    examples…"), while "Python examples" and "Rust examples" stay apart (60%)."""
    wa, wb = _content_words(a), _content_words(b)
    if not wa or not wb:
        return " ".join(a.lower().split()) == " ".join(b.lower().split())
    return len(wa & wb) / len(wa | wb) >= 0.75


def _find(db: Session, memory_id: str, project_id: Optional[str], any_scope: bool = False) -> tuple[Optional[Memory], str]:
    key = (memory_id or "").strip().strip("[]").replace("-", "").lower()
    if len(key) < 4:
        return None, "Error: give the memory's id as shown in the list (8 characters)."
    stmt = select(Memory) if any_scope else _scope_filter(select(Memory), project_id)
    rows = [m for m in db.exec(stmt).all() if m.id.replace("-", "").lower().startswith(key)]
    if not rows:
        return None, f"Error: no memory with id '{memory_id}' here."
    if len(rows) > 1:
        return None, f"Error: '{memory_id}' matches more than one memory; use more of the id."
    return rows[0], ""


# "forget", "stop remembering", "remove/delete ... memory", "no longer true" -- but not "don't
# forget to ...", which is a request to remember. A correction ("that's wrong, it's X") is not a
# forget request: it goes through remember(replaces=<id>). 2026-09-29: "i gave the wrong name bro
# its actually called qwen" was read as one and the user's only preference was deleted.
_FORGET_REQUEST_RE = re.compile(
    r"(?<!don't )(?<!dont )(?<!do not )(?<!never )(?<!not )\bforget\b(?!\s+to\b)|"
    r"\bunremember\b|\bstop remembering\b|\bdon'?t remember\b|"
    r"\bno longer (?:true|the case)\b|\bnot (?:true|the case) any ?more\b|"
    r"\b(?:delete|remove|erase|drop|clear|wipe)\b[^.?!\n]{0,40}\bmemor(?:y|ies)\b|"
    r"\bmemor(?:y|ies)\b[^.?!\n]{0,40}\b(?:delete|remove|erase|drop|clear|wipe)\b",
    re.IGNORECASE,
)


def asks_to_forget(user_message: str) -> bool:
    """Does the user's message ask for something saved to be forgotten?"""
    return bool(_FORGET_REQUEST_RE.search(user_message or ""))


# Facts forgotten in the current turn, per session: re-saving one of them in the same reply is
# the forget/remember loop from 2026-09-29 (forget -> remember -> forget -> ... ending deleted).
_forgotten_this_turn: dict[str, tuple[str, list[str]]] = {}


def _turn_key(user_message: Optional[str]) -> str:
    return " ".join((user_message or "").split())[:500]


def _note_forgotten(session_id: Optional[str], user_message: Optional[str], content: str) -> None:
    if not session_id:
        return
    key = _turn_key(user_message)
    prev_key, contents = _forgotten_this_turn.get(session_id, ("", []))
    _forgotten_this_turn[session_id] = (key, (contents if prev_key == key else []) + [content])


def _forgotten_earlier_this_turn(session_id: Optional[str], user_message: Optional[str], text: str) -> bool:
    if not session_id or session_id not in _forgotten_this_turn:
        return False
    key, contents = _forgotten_this_turn[session_id]
    return key == _turn_key(user_message) and any(_same_fact(c, text) for c in contents)


def remember(
    content: str,
    kind: str = "project",
    replaces: str = "",
    project_id: Optional[str] = None,
    session_id: Optional[str] = None,
    user_message: Optional[str] = None,
) -> str:
    """Save a lasting fact for future conversations (only what will still matter later -- never what the files already say, never passing chat); pass replaces=<id> to correct an existing memory instead of adding a near-duplicate.

    kind: about_user (who the user is), preference (how they like things), workflow (how to work
    with them), project (facts/conventions of this project), decision (what was settled and why),
    task (open work to pick up later). The first three are global; the rest belong to the workspace (general facts outside one).
    """
    text = " ".join((content or "").split())
    if not text:
        return "Error: nothing to remember."
    if len(text) > MEMORY_MAX_CHARS:
        return f"Error: keep a memory under {MEMORY_MAX_CHARS} characters -- one fact, stated plainly."
    kind = (kind or "project").strip().lower()
    if kind not in MEMORY_KINDS:
        return f"Error: kind must be one of {', '.join(MEMORY_KINDS)}."
    if _is_ephemeral(session_id):
        return "Not saved: this is an off-the-record conversation, and nothing from it is kept."
    if _forgotten_earlier_this_turn(session_id, user_message, text):
        return ("Not saved: you forgot this same fact earlier in this reply. Leave it forgotten; "
                "if it should stay, tell the user and let them decide.")
    category, project_bound = MEMORY_KINDS[kind]
    if project_bound and not project_id:
        # No project to file it under (Freeform): keep it as a general fact rather than lose it --
        # "the client's name is Acme" was refused three times running when this said no.
        category = "recurring_fact"
    scope = project_id if project_bound else None
    now = datetime.utcnow()

    with session_factory() as db:
        if replaces:
            row, err = _find(db, replaces, project_id)
            if row is None:
                return err
            row.content, row.category, row.project_id, row.updated_at = text, category, scope, now
            row.source_session_id = session_id or row.source_session_id
            db.add(row)
            db.commit()
            logger.info("Memory %s replaced (%s)", short_id(row.id), category)
            return f"Updated memory [{short_id(row.id)}]."

        same_scope = select(Memory).where(
            col(Memory.project_id) == scope if scope else col(Memory.project_id).is_(None))
        existing = db.exec(same_scope).all()
        for m in existing:
            if _same_fact(m.content, text):
                return f"Already remembered as [{short_id(m.id)}]: {m.content}"
        if len(existing) >= MEMORY_LIMIT_PER_SCOPE:
            return (f"Not saved: {MEMORY_LIMIT_PER_SCOPE} memories is the limit here. Replace an outdated "
                    "one (replaces=<id>) or forget one first.")

        row = Memory(
            id=str(uuid.uuid4()), project_id=scope, category=category, content=text,
            source_session_id=session_id, confidence=1.0, pinned=False,
            created_at=now, updated_at=now, last_used_at=None,
        )
        db.add(row)
        db.commit()
        logger.info("Memory %s saved (%s, %s)", short_id(row.id), category, "project" if scope else "global")
        where = "for this project" if scope else "about the user (all conversations)"
        return f"Remembered [{short_id(row.id)}] {where}."


def forget(
    memory_id: str,
    project_id: Optional[str] = None,
    session_id: Optional[str] = None,
    user_message: Optional[str] = None,
) -> str:
    """Forget a saved memory by its id -- only when the user asks for it to be forgotten; to correct a memory use remember(replaces=<id>) instead."""
    if _is_ephemeral(session_id):
        return "Not changed: this is an off-the-record conversation; memories are left as they are."
    # user_message is None only outside a chat turn (API, tests calling the function directly).
    if user_message is not None and not asks_to_forget(user_message):
        return ("Not forgotten: the user didn't ask to forget anything in this message. Leave the "
                "memory as it is; if it is wrong, correct it with remember(replaces=<id>), or ask "
                "the user whether to forget it.")
    from app.memory import memory_bin

    with session_factory() as db:
        row, err = _find(db, memory_id, project_id)
        if row is None:
            return err
        sid, content = short_id(row.id), row.content
        memory_bin.add(row, reason=(user_message or "")[:200], session_id=session_id)
        db.delete(row)
        db.commit()
    _note_forgotten(session_id, user_message, content)
    logger.info("Memory %s forgotten (kept in the bin for %d days)", sid, memory_bin.KEEP_DAYS)
    return f"Forgot [{sid}]: {content}"


def undo_remember(memory_id: str) -> Optional[str]:
    """The Undo on a "Remembered: ..." chat line: move that memory to the bin (so the undo can
    itself be undone) by the short id the line shows, in any scope. Returns its content, or
    None when no single memory matches."""
    from app.memory import memory_bin

    with session_factory() as db:
        row, _ = _find(db, memory_id, None, any_scope=True)
        if row is None:
            return None
        content = row.content
        memory_bin.add(row, reason="Undo in chat")
        db.delete(row)
        db.commit()
    logger.info("Memory %s removed by Undo (kept in the bin)", short_id(memory_id))
    return content
