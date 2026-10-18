"""
Notes and a task list as plain Markdown files the user owns (PLAN 4.16 phase A).

They live in one folder (``JARVIS_NOTES_DIR``, default ``Documents\\Jarvis Notes``) that the user
also opens as an Obsidian vault, so everything here is ordinary Markdown that a person may edit by
hand at any time: a note is ``<title>.md``; ``tasks.md`` is a checklist,

    - [ ] Pay rent (due 2026-10-20 17:00) <!-- id:t3f9a rem:rtn_... -->

The HTML comment (hidden in Obsidian's reading view) carries the task's id and the reminder its
due time set. A line typed by hand has no id yet: it gets one derived from its text until the next
write stores a real one, so the file is never rewritten just to be read.

Off-the-record (ephemeral) chats may read but never write (the user's call, 2026-10-18, like
``remember``). One tool per area with an ``action`` argument: every tool Freeform offers costs
accuracy on unrelated turns (4.15's eval), so the schemas stay short.
"""
import hashlib
import logging
import os
import re
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from app.config import settings

logger = logging.getLogger("jarvis.agent.tools.notes")

TASKS_FILE = "tasks.md"
_SKIP_DIRS = {".obsidian", ".trash", ".git"}
_lock = threading.Lock()
_OFF_RECORD = "Not saved: this is an off-the-record conversation, and nothing from it is kept."


# --- the folder ---------------------------------------------------------------------------------

def _documents_dir() -> Path:
    """The real Documents folder: on this laptop OneDrive redirects it, so ~/Documents is wrong."""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [("d1", wintypes.DWORD), ("d2", wintypes.WORD), ("d3", wintypes.WORD),
                            ("d4", ctypes.c_ubyte * 8)]

            # FOLDERID_Documents {FDD39AD0-238F-46AF-ADB4-6C85480369C7}
            fid = GUID(0xFDD39AD0, 0x238F, 0x46AF, (ctypes.c_ubyte * 8)(0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7))
            out = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(fid), 0, None, ctypes.byref(out)) == 0:
                path = out.value
                ctypes.windll.ole32.CoTaskMemFree(out)
                if path:
                    return Path(path)
        except Exception as exc:  # pragma: no cover - Windows API hiccup
            logger.debug("Known-folder lookup failed: %s", exc)
    return Path.home() / "Documents"


def notes_dir() -> Path:
    return Path(settings.notes_dir).expanduser() if settings.notes_dir else _documents_dir() / "Jarvis Notes"


def _is_ephemeral(session_id: Optional[str]) -> bool:
    from app.agent.tools.memory import _is_ephemeral as check
    return check(session_id)


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


# --- notes --------------------------------------------------------------------------------------

_BAD_NAME = re.compile(r'[*"\\/<>:|?#^\[\]\x00-\x1f]')  # Windows + Obsidian (# ^ [ ] | break links)


def _file_name(title: str) -> str:
    name = " ".join(_BAD_NAME.sub(" ", title or "").split()).strip(" .")
    return name[:100].rstrip(" .")


def _all_notes(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    found = []
    for p in folder.rglob("*.md"):
        rel = p.relative_to(folder)
        if any(part in _SKIP_DIRS for part in rel.parts[:-1]) or (len(rel.parts) == 1 and p.name == TASKS_FILE):
            continue
        found.append(p)
    return found


def _find_note(folder: Path, title: str) -> tuple[Optional[Path], str]:
    """A note by title: the exact name (any folder, any case), else the one name containing it."""
    want = _file_name(title).lower()
    if not want:
        return None, "Error: which note? Give its title."
    notes = _all_notes(folder)
    exact = [p for p in notes if p.stem.lower() == want]
    if len(exact) == 1:
        return exact[0], ""
    partial = exact or [p for p in notes if want in p.stem.lower()]
    if len(partial) == 1:
        return partial[0], ""
    if not partial:
        return None, f"Error: no note called '{title}'. notes(action='list') shows them, or 'find' searches inside."
    names = ", ".join(sorted(p.stem for p in partial)[:8])
    return None, f"Error: '{title}' matches several notes ({names}). Use the full title."


def _label(folder: Path, p: Path) -> str:
    rel = p.relative_to(folder).with_suffix("")
    return rel.as_posix()


def notes(action: str = "list", title: str = "", text: str = "", query: str = "",
          session_id: Optional[str] = None) -> str:
    """
    The user's notes (Markdown files in their notes folder): add a new note, append to one, find notes about something, read one, or list them. Personal to-dos go to tasks, not here.

    Args:
        action: add, append, find, read or list.
        title: The note's title (add, append, read).
        text: What to write (add, append).
        query: Words to look for (find).
    """
    act = str(action or "list").strip().lower()
    folder = notes_dir()
    if act == "list":
        found = sorted(_all_notes(folder), key=lambda p: p.stat().st_mtime, reverse=True)
        if not found:
            return f"No notes yet (folder: {folder})."
        lines = [f"- {_label(folder, p)} ({datetime.fromtimestamp(p.stat().st_mtime):%d %b %H:%M})" for p in found[:30]]
        more = f"\n... and {len(found) - 30} more" if len(found) > 30 else ""
        return f"{len(found)} notes, newest first:\n" + "\n".join(lines) + more
    if act == "read":
        path, err = _find_note(folder, title)
        if path is None:
            return err
        body = path.read_text(encoding="utf-8", errors="replace")
        if len(body) > 8000:
            body = body[:8000] + "\n... (cut here; the note is longer)"
        return f"Note '{_label(folder, path)}':\n{body}"
    if act == "find":
        words = [w for w in re.findall(r"\w+", (query or title).lower()) if len(w) > 1]
        if not words:
            return "Error: find needs some words to look for (query)."
        scored = []
        for p in _all_notes(folder):
            body = p.read_text(encoding="utf-8", errors="replace")
            hay = (p.stem + "\n" + body).lower()
            hits = sum(1 for w in words if w in hay)
            if hits:
                lines = [ln.strip() for ln in body.splitlines() if any(w in ln.lower() for w in words)]
                if not lines:  # matched on the title ("dentist" -> Dentist.md): show how it begins
                    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
                scored.append((hits, p.stat().st_mtime, p, lines[:3]))
        if not scored:
            return f"No note mentions '{query or title}'."
        scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
        out = []
        for _, _, p, lines in scored[:8]:
            out.append(f"- {_label(folder, p)}" + "".join(f"\n    {ln[:200]}" for ln in lines))
        return "\n".join(out)
    if act in ("add", "append"):
        if _is_ephemeral(session_id):
            return _OFF_RECORD
        body = (text or "").strip()
        if not body:
            return f"Error: {act} needs the text to write."
        name = _file_name(title)
        if not name:
            if act == "append":
                return "Error: append needs the title of the note to add to."
            name = _file_name(" ".join(body.split()[:6])) or datetime.now().strftime("Note %Y-%m-%d %H%M")
        with _lock:
            existing = None
            if act == "append":
                existing, _ = _find_note(folder, name)
            else:
                existing = next((p for p in _all_notes(folder) if p.stem.lower() == name.lower()), None)
                if existing is not None:
                    return (f"Error: a note called '{existing.stem}' already exists. Use action='append' to add to it, "
                            "or pick another title.")
            if existing is not None:
                old = existing.read_text(encoding="utf-8", errors="replace").rstrip("\n")
                _write_atomic(existing, (old + "\n\n" if old else "") + body + "\n")
                return f"Added to the note '{_label(folder, existing)}'."
            path = folder / f"{name}.md"
            _write_atomic(path, body + "\n")
            return f"Saved the note '{name}'."
    return "Error: action must be add, append, find, read or list."


# --- the per-turn cue ---------------------------------------------------------------------------

# Eval 2026-10-18 (Defiant, 3 seeds): "note that the guest wifi password is on the router" -> "Noted,
# sir." and "I bought the milk" -> "The milk is noted.", 0/3 each, no tool. The schemas alone don't
# make the 9B reach for these on a conversational message, so code points at the tool for this turn.
_ASKS_NOTE = re.compile(
    r"\b(?:note (?:that|down|this|it)|make a note|take a note|jot (?:down|this|that|it)|"
    r"write (?:this|that|it) down|(?:add|put|save) (?:(?:this|that|it) )?(?:to|in) (?:my|the) notes?)\b", re.I)
_SAYS_DONE = re.compile(
    r"\b(?:i(?:'ve| have)?|we(?:'ve| have)?)\s+(?:just\s+|finally\s+|already\s+)?"
    r"(?:bought|got|did|done|finished|paid|called|rang|emailed|sent|booked|picked up|cleaned|fixed|submitted|"
    r"returned|posted|ordered|renewed|cancelled|handed in|took)\b|\b(?:is|are|'s) (?:done|sorted|finished)\b", re.I)
_STOP = frozenset("the a an my to of for and with on in at it this that just i we have has did got".split())


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2 and w not in _STOP}


def turn_directive(user_message: str) -> str:
    """A one-line pointer for this turn when the message plainly asks for a note, or says that an
    open task (sharing a word with the message) is done; "" otherwise. Rides on the user turn."""
    text = user_message or ""
    if _ASKS_NOTE.search(text):
        return ("NOTES: the user asked for a note. Write it with notes(action='add', or 'append' to an "
                "existing note); replying \"noted\" without the tool saves nothing.")
    if _SAYS_DONE.search(text):
        try:
            _, lines = _read_tasks()
        except OSError:
            return ""
        said = _words(text)
        hits = [t for t in _parse_tasks(lines) if not t.done and said & _words(t.text)]
        if len(hits) == 1:
            t = hits[0]
            return (f"TASKS: the open task \"{t.text}\" (id {t.id}) sounds done. If it is, tick it off with "
                    f"tasks(action='done', task_id='{t.id}').")
    return ""


# --- tasks --------------------------------------------------------------------------------------

_TASK_LINE = re.compile(r"^(?P<indent>\s*)[-*] \[(?P<mark>[ xX])\] (?P<rest>.*)$")
_META = re.compile(r"\s*<!--\s*(?P<meta>.*?)\s*-->\s*$")
_DUE = re.compile(r"\s*\(due (?P<date>\d{4}-\d{2}-\d{2})(?: (?P<time>\d{2}:\d{2}))?\)")
_DONE = re.compile(r"\s*\(done \d{4}-\d{2}-\d{2}\)")
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


class _Task:
    def __init__(self, index: int, indent: str, done: bool, text: str, due: str, due_time: str,
                 tid: str, reminder: str, stored_id: bool):
        self.index, self.indent, self.done, self.text = index, indent, done, text
        self.due, self.due_time, self.id, self.reminder, self.stored_id = due, due_time, tid, reminder, stored_id

    def due_label(self) -> str:
        return f"{self.due} {self.due_time}".strip()

    def render(self) -> str:
        due = f" (due {self.due_label()})" if self.due else ""
        done = f" (done {datetime.now():%Y-%m-%d})" if self.done else ""
        meta = f"id:{self.id}" + (f" rem:{self.reminder}" if self.reminder else "")
        return f"{self.indent}- [{'x' if self.done else ' '}] {self.text}{due}{done} <!-- {meta} -->"


def _derived_id(text: str) -> str:
    return "h" + hashlib.sha1(" ".join(text.lower().split()).encode()).hexdigest()[:4]


def _parse_tasks(lines: list[str]) -> list[_Task]:
    tasks = []
    for i, line in enumerate(lines):
        m = _TASK_LINE.match(line)
        if not m:
            continue
        rest, meta = m.group("rest"), {}
        mm = _META.search(rest)
        if mm:
            rest = rest[:mm.start()]
            for part in mm.group("meta").split():
                if ":" in part:
                    k, v = part.split(":", 1)
                    meta[k] = v
        due, due_time = "", ""
        dm = _DUE.search(rest)
        if dm:
            due, due_time = dm.group("date"), dm.group("time") or ""
            rest = rest[:dm.start()] + rest[dm.end():]
        rest = _DONE.sub("", rest).strip()
        tid = meta.get("id") or _derived_id(rest)
        tasks.append(_Task(i, m.group("indent"), m.group("mark") != " ", rest, due, due_time, tid,
                           meta.get("rem", ""), "id" in meta))
    return tasks


def _read_tasks() -> tuple[Path, list[str]]:
    path = notes_dir() / TASKS_FILE
    if not path.is_file():
        return path, []
    return path, path.read_text(encoding="utf-8", errors="replace").splitlines()


def _save_tasks(path: Path, lines: list[str], tasks: list[_Task]) -> None:
    for t in tasks:
        if not t.stored_id:
            t.id = _new_id(tasks)
            t.stored_id = True
        lines[t.index] = t.render()
    if not lines or not any(ln.startswith("#") for ln in lines[:3]):
        lines = ["# Tasks", ""] + lines
    _write_atomic(path, "\n".join(lines).rstrip("\n") + "\n")


def _new_id(tasks: list[_Task]) -> str:
    taken = {t.id for t in tasks}
    while True:
        tid = "t" + uuid.uuid4().hex[:4]
        if tid not in taken:
            return tid


def _now() -> datetime:
    return datetime.now()


def parse_due(due: str, now: Optional[datetime] = None) -> tuple[str, str, Optional[str]]:
    """(date, time, error). A day alone ("tomorrow", "friday", "2026-10-20") is a due date with no
    reminder; anything with a time ("5pm", "tomorrow 9am", "in 2 hours") also sets a reminder."""
    from app.agent.tools.reminders import parse_when

    now = now or _now()
    text = " ".join(str(due or "").lower().replace(",", " ").split())
    if not text:
        return "", "", None
    day = re.sub(r"^(?:on|by|due)\s+", "", text)
    if day == "today":
        return f"{now:%Y-%m-%d}", "", None
    if day == "tomorrow":
        return f"{now + timedelta(days=1):%Y-%m-%d}", "", None
    wd = re.fullmatch(r"(?:next\s+)?(" + "|".join(_WEEKDAYS) + r")", day)
    if wd:
        ahead = (_WEEKDAYS.index(wd.group(1)) - now.weekday()) % 7 or 7
        return f"{now + timedelta(days=ahead):%Y-%m-%d}", "", None
    try:
        return datetime.strptime(day, "%Y-%m-%d").strftime("%Y-%m-%d"), "", None
    except ValueError:
        pass
    at = parse_when(day, now)
    if at is None:
        return "", "", (f"Error: couldn't read the due time '{due}'. Use e.g. 'tomorrow', 'friday', "
                        "'2026-10-20', '5pm', 'tomorrow 9am' or 'in 2 hours'.")
    return f"{at:%Y-%m-%d}", f"{at:%H:%M}", None


def _scheduler():
    from app.main import routine_scheduler
    return routine_scheduler


def _set_reminder(task: _Task) -> str:
    """Schedule the due-time reminder for an open task with a time in the future; '' if none."""
    if task.done or not task.due or not task.due_time:
        return ""
    at = datetime.strptime(f"{task.due} {task.due_time}", "%Y-%m-%d %H:%M")
    if at <= _now():
        return ""
    from app.routines.models import Routine, RoutineKind

    message = f"Due now: {task.text}"
    routine = Routine(name=message[:60], time=at.strftime("%H:%M"), kind=RoutineKind.MESSAGE, message=message,
                      reminder=True, at=at.strftime("%Y-%m-%dT%H:%M"))
    try:
        _scheduler().create(routine)
    except Exception as exc:
        logger.warning("Task reminder not scheduled: %s", exc)
        return ""
    return routine.id


def _cancel_reminder(task: _Task) -> None:
    if task.reminder:
        try:
            _scheduler().delete(task.reminder)
        except Exception as exc:
            logger.debug("Task reminder %s not cancelled: %s", task.reminder, exc)
        task.reminder = ""


def _match(tasks: list[_Task], key: str, want_done: Optional[bool] = None) -> tuple[Optional[_Task], str]:
    """A task by id, else by words of its text ("milk" for "Buy milk"); one match or an error."""
    k = " ".join(str(key or "").split()).lower()
    if not k:
        return None, "Error: which task? Give task_id (from the list) or some of its words."
    pool = [t for t in tasks if want_done is None or t.done == want_done]
    by_id = [t for t in tasks if t.id.lower() == k]
    if len(by_id) == 1:
        return by_id[0], ""
    hits = [t for t in pool if k in t.text.lower()]
    if not hits:
        words = re.findall(r"\w+", k)
        hits = [t for t in pool if words and all(w in t.text.lower() for w in words)]
    if len(hits) == 1:
        return hits[0], ""
    if not hits:
        return None, f"Error: no task matches '{key}'. tasks(action='list') shows them with ids."
    return None, "Error: several tasks match: " + "; ".join(f"{t.id}: {t.text}" for t in hits[:6]) + ". Use the id."


def _describe(t: _Task) -> str:
    due = f" (due {t.due_label()})" if t.due else ""
    return f"{t.id}: {t.text}{due}"


def tasks(action: str = "list", text: str = "", due: str = "", task_id: str = "",
          session_id: Optional[str] = None) -> str:
    """
    The user's to-do list (tasks.md in their notes folder): add a task, list what's open, mark one done, reopen, edit or remove it. A due time also sets a reminder.

    Args:
        action: add, list, done, reopen, edit or remove.
        text: The task (add), or its new wording (edit).
        due: When it's due (add, edit): 'tomorrow', 'friday', '2026-10-20', '5pm', 'tomorrow 9am'.
        task_id: Which task (done, reopen, edit, remove): its id from the list, or some of its words.
    """
    act = str(action or "list").strip().lower()
    if act not in ("add", "list", "done", "reopen", "edit", "remove"):
        return "Error: action must be add, list, done, reopen, edit or remove."
    with _lock:
        path, lines = _read_tasks()
        items = _parse_tasks(lines)
        if act == "list":
            open_ = [t for t in items if not t.done]
            done_n = len(items) - len(open_)
            if not open_:
                return "Nothing open on the list." + (f" ({done_n} done.)" if done_n else "")
            today = f"{_now():%Y-%m-%d}"
            open_.sort(key=lambda t: (not t.due, t.due_label()))
            out = [f"- {_describe(t)}" + (" OVERDUE" if t.due and t.due < today else "") for t in open_]
            return f"{len(open_)} open:\n" + "\n".join(out) + (f"\n({done_n} done.)" if done_n else "")

        if _is_ephemeral(session_id):
            return _OFF_RECORD
        if act == "add":
            body = " ".join(str(text or "").split())
            if not body:
                return "Error: add needs the task's text."
            dup = next((t for t in items if not t.done and t.text.lower() == body.lower()), None)
            if dup is not None:
                return f"Already on the list: {_describe(dup)}."
            d, tm, err = parse_due(due)
            if err:
                return err
            task = _Task(len(lines), "", False, body, d, tm, _new_id(items), "", True)
            lines.append("")
            items.append(task)
            task.reminder = _set_reminder(task)
            _save_tasks(path, lines, items)
            note = " I'll remind you then." if task.reminder else (
                " That time has already passed, so no reminder." if tm else "")
            return f"Added {_describe(task)}.{note}"

        task, err = _match(items, task_id or text, {"done": False, "reopen": True}.get(act))
        if task is None:
            return err
        if act == "done":
            task.done = True
            _cancel_reminder(task)
            _save_tasks(path, lines, items)
            return f"Ticked off: {task.text}."
        if act == "reopen":
            task.done = False
            task.reminder = _set_reminder(task)
            _save_tasks(path, lines, items)
            return f"Back on the list: {_describe(task)}."
        if act == "remove":
            _cancel_reminder(task)
            del lines[task.index]
            items = _parse_tasks(lines)
            _save_tasks(path, lines, items)
            return f"Removed: {task.text}."
        # edit
        new_text = " ".join(str(text or "").split()) if task_id else ""
        if not new_text and not due:
            return "Error: edit needs new text (with task_id naming the task) or a new due time."
        if due:
            d, tm, err = parse_due(due)
            if err:
                return err
            task.due, task.due_time = d, tm
        if new_text:
            task.text = new_text
        _cancel_reminder(task)
        task.reminder = _set_reminder(task)
        _save_tasks(path, lines, items)
        return f"Updated: {_describe(task)}."
