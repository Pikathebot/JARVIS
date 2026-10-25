"""
A project's living state (PLAN 4.18): ``.jarvis/PROJECT.md`` in the workspace folder -- the
user's own folder for an external workspace, Jarvis's project dir for an internal one -- with
five sections: Goal, Now, Next, Done, Decisions.

It is the Jarvis counterpart of the CLAUDE.md / PLAN.md a Claude Code session reads first: what
the project is for, where it stands, what comes next and what was settled. A chat reads it once,
at its first turn, into the head of the prompt beside the project instructions and keeps that
copy for the whole chat (``snapshot``), so the cached prefix stays byte-stable; an edit shows
from the next chat or after a compaction (``drop_snapshot``).

Every change goes through ``edit`` -- one section per call, never a rewrite of the whole file --
so the background keeper and the model can't overwrite what the user wrote by hand. The file is
backed up before each write and kept under ``STATE_MAX_CHARS``: the oldest Done lines go first,
Decisions are never trimmed.
"""
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.agent.tools.file_safety import backup_file
from app.workspaces import jarvis_project_dir, workspaces_root

logger = logging.getLogger("jarvis.project_state")

STATE_FILE = Path(".jarvis") / "PROJECT.md"
STATE_MAX_CHARS = 2000
SECTIONS = ("Goal", "Now", "Next", "Done", "Decisions")
ACTIONS = ("set_now", "add_next", "tick", "add_done", "add_decision")
ITEM_MAX_CHARS = 200

_HEADING = re.compile(r"^##\s+(.+?)\s*$")
_CHECKBOX = re.compile(r"^-\s*\[[ xX]\]\s*")
_DATED = re.compile(r"^-\s*\d{4}-\d{2}-\d{2}\s+")

# session id -> the head block that chat read at its first turn
_snapshots: dict[str, str] = {}


def workspace_dir(workspace_path: Optional[str], project_id: str) -> Path:
    if isinstance(workspace_path, str) and workspace_path.strip():
        path = Path(workspace_path).resolve()
        # The shared root is every project's parent, not this one's folder.
        if path != workspaces_root():
            return path
    return jarvis_project_dir(project_id)


def state_path(workspace_path: Optional[str], project_id: str) -> Path:
    return workspace_dir(workspace_path, project_id) / STATE_FILE


def template(name: str, goal: Optional[str] = None) -> str:
    goal_text = " ".join((goal or "").split()) or "(not set yet -- what this project is for, and what done looks like)"
    return (
        f"# {name.strip() or 'Project'}\n\n"
        f"## Goal\n{goal_text}\n\n"
        "## Now\n\n"
        "## Next\n\n"
        "## Done\n\n"
        "## Decisions\n"
    )


def parse(text: str) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """(lines before the first ``##`` heading, [(heading, body lines)] in file order). Headings the
    template doesn't know are kept as they are."""
    preamble: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        m = _HEADING.match(line)
        if m:
            sections.append((m.group(1), []))
        elif sections:
            sections[-1][1].append(line)
        else:
            preamble.append(line)
    return preamble, sections


def render(preamble: list[str], sections: list[tuple[str, list[str]]]) -> str:
    out = list(preamble)
    while out and not out[-1].strip():
        out.pop()
    for heading, body in sections:
        lines = list(body)
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        if out:
            out.append("")
        out.append(f"## {heading}")
        out.extend(lines)
    return "\n".join(out) + "\n"


def _section(sections: list[tuple[str, list[str]]], name: str) -> list[str]:
    """The body of section ``name`` (case-insensitive), added in template order if missing."""
    for heading, body in sections:
        if heading.strip().lower() == name.lower():
            return body
    body: list[str] = []
    order = [s.lower() for s in SECTIONS]
    rank = order.index(name.lower())
    at = len(sections)
    for i, (heading, _) in enumerate(sections):
        h = heading.strip().lower()
        if h in order and order.index(h) > rank:
            at = i
            break
    sections.insert(at, (name, body))
    return body


def _items(body: list[str]) -> list[str]:
    return [line for line in body if line.strip().startswith("-")]


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


def read(workspace_path: Optional[str], project_id: str) -> str:
    try:
        return state_path(workspace_path, project_id).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def ensure(workspace_path: Optional[str], project_id: str, name: str, goal: Optional[str] = None) -> Path:
    """The file, created from the template if it isn't there yet."""
    path = state_path(workspace_path, project_id)
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(template(name, goal), encoding="utf-8")
        logger.info("Created %s", path)
    return path


MOVED_CATEGORIES = {"important_decision": "Decisions", "persistent_task_context": "Next"}


def ensure_with_memories(session_factory, workspace_path: Optional[str], project_id: str, name: str,
                         goal: Optional[str] = None) -> Path:
    """``ensure``, and when that creates the file, the project's decision and open-task memories
    move into it (PLAN 4.18 decision 4: one place for them) and leave the database."""
    path = state_path(workspace_path, project_id)
    if path.is_file():
        return path
    path = ensure(workspace_path, project_id, name, goal)
    from sqlmodel import col, select
    from app.database.models import Memory
    try:
        with session_factory() as db:
            rows = db.exec(select(Memory).where(col(Memory.project_id) == project_id)
                           .where(col(Memory.category).in_(list(MOVED_CATEGORIES)))
                           .order_by(col(Memory.created_at))).all()
            if not rows:
                return path
            preamble, sections = parse(read(workspace_path, project_id))
            for row in rows:
                body = _section(sections, MOVED_CATEGORIES[row.category])
                text = " ".join(row.content.split())
                if MOVED_CATEGORIES[row.category] == "Next":
                    body.append(f"- [ ] {text}")
                else:
                    body.insert(_first_item(body), f"- {row.created_at:%Y-%m-%d} {text}")
            new_text = _fit(sections, preamble) or render(preamble, sections)
            path.write_text(new_text, encoding="utf-8")
            for row in rows:
                db.delete(row)
            db.commit()
            logger.info("Moved %d decision/task memories of %s into %s", len(rows), project_id, path)
    except Exception as e:
        logger.warning("Memories of %s not moved into PROJECT.md: %s", project_id, e)
    return path


def write(workspace_path: Optional[str], project_id: str, text: str) -> Path:
    """Replace the whole file (the user's own edit from the project page), backed up first."""
    path = state_path(workspace_path, project_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_file(path, workspace_dir(workspace_path, project_id))
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    return path


def _fit(sections: list[tuple[str, list[str]]], preamble: list[str]) -> Optional[str]:
    """Trim the oldest Done lines until the file fits; None when it can't."""
    text = render(preamble, sections)
    done = _section(sections, "Done")
    while len(text) > STATE_MAX_CHARS:
        idx = [i for i, line in enumerate(done) if line.strip().startswith("-")]
        if not idx:
            return None
        del done[idx[-1]]
        text = render(preamble, sections)
    return text


def edit(workspace_path: Optional[str], project_id: str, name: str, action: str, text: str,
         today: Optional[str] = None) -> str:
    """Apply one section edit and save. Returns a one-line result for the model (``Error: ...``
    when nothing was changed)."""
    action = (action or "").strip().lower()
    if action not in ACTIONS:
        return f"Error: action must be one of {', '.join(ACTIONS)}."
    # One line of text: a list marker, checkbox or date the model added is the file's job.
    item = " ".join((text or "").split())
    item = _CHECKBOX.sub("", "- " + item.lstrip("-* ").strip())
    item = re.sub(r"^\d{4}-\d{2}-\d{2}\s+", "", item.lstrip("- ").strip())
    if not item:
        return "Error: say what to write."
    if len(item) > ITEM_MAX_CHARS:
        return f"Error: keep it under {ITEM_MAX_CHARS} characters -- one line."
    date = today or datetime.now().strftime("%Y-%m-%d")

    path = ensure(workspace_path, project_id, name)
    preamble, sections = parse(read(workspace_path, project_id))

    if action == "set_now":
        body = _section(sections, "Now")
        body[:] = [item]
        result = "Now set."
    elif action == "add_next":
        body = _section(sections, "Next")
        if any(_norm(_CHECKBOX.sub("", line.strip())) == _norm(item) for line in _items(body)):
            return "Already in Next."
        body.append(f"- [ ] {item}")
        result = "Added to Next."
    elif action == "tick":
        body = _section(sections, "Next")
        want = _norm(item)
        open_items = [(i, line) for i, line in enumerate(body) if line.strip().startswith("-")]
        match = [(i, line) for i, line in open_items
                 if want and (want in _norm(_CHECKBOX.sub("", line.strip())) or _norm(_CHECKBOX.sub("", line.strip())) in want)]
        if not match:
            listed = "; ".join(_CHECKBOX.sub("", line.strip()) for _, line in open_items) or "nothing"
            return f"Error: no Next item matches that. Next has: {listed}."
        if len(match) > 1:
            return "Error: that matches more than one Next item -- quote more of it."
        i, line = match[0]
        done_text = _CHECKBOX.sub("", line.strip())
        del body[i]
        done = _section(sections, "Done")
        done.insert(_first_item(done), f"- {date} {done_text}")
        result = "Ticked off and moved to Done."
    elif action == "add_done":
        done = _section(sections, "Done")
        done.insert(_first_item(done), f"- {date} {item}")
        result = "Added to Done."
    else:  # add_decision
        decisions = _section(sections, "Decisions")
        if any(_norm(_DATED.sub("", line.strip())) == _norm(item) for line in _items(decisions)):
            return "Already in Decisions."
        decisions.insert(_first_item(decisions), f"- {date} {item}")
        result = "Added to Decisions."

    new_text = _fit(sections, preamble)
    if new_text is None:
        return (f"Error: PROJECT.md is full ({STATE_MAX_CHARS} characters) and has no Done lines left to "
                "trim. Ask the user to shorten it.")
    backup_file(path, workspace_dir(workspace_path, project_id))
    path.write_text(new_text, encoding="utf-8")
    logger.info("PROJECT.md %s for project %s", action, project_id)
    return result


def _first_item(body: list[str]) -> int:
    """Where a newest-first line goes: before the first list item (after any prose)."""
    for i, line in enumerate(body):
        if line.strip().startswith("-"):
            return i
    return len(body)


def head_block(instructions: Optional[str], state_text: str) -> str:
    """The project's part of the head: its instructions (rules) and PROJECT.md (state)."""
    parts = []
    if instructions and instructions.strip():
        parts.append(f"--- Project Instructions ---\n{instructions.strip()}")
    state = state_text.strip()
    if state:
        if len(state) > STATE_MAX_CHARS:
            state = state[:STATE_MAX_CHARS].rstrip() + "\n...(cut: PROJECT.md is over its size)"
        parts.append(
            "--- Project State (.jarvis/PROJECT.md, as of the start of this chat; keep it current with "
            f"project_state) ---\n{state}")
    return "\n\n".join(parts)


def snapshot(session_id: str, build) -> str:
    """The head block this chat read at its first turn; ``build()`` makes it the first time."""
    if session_id not in _snapshots:
        _snapshots[session_id] = build()
    return _snapshots[session_id]


def drop_snapshot(session_id: str) -> None:
    """After a compaction the prefix is rebuilt anyway: the next turn reads the file again."""
    _snapshots.pop(session_id, None)
