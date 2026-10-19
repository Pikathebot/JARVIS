"""tasks.md <-> Google Tasks' default list ("My Tasks"), two-way (PLAN 4.16 phase B).

tasks.md stays the file the user edits (Obsidian, Notepad, Jarvis). The link between a line and a
Google task lives in ``google_tasks_sync.json`` beside the token (local id -> Google id, plus what
each side looked like at the last sync), so the Markdown carries nothing new.

One sync: fetch every Google task, compare both sides with the last sync, then
- changed on one side only -> copy it to the other;
- changed on both -> the newer wins (tasks.md's modified time vs Google's ``updated``), Google on a tie;
- new on one side -> created on the other; deleted on one side and untouched on the other -> deleted.
Google Tasks keeps a due *date* only; a local due time stays local (and keeps its reminder).
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.google import api, auth

logger = logging.getLogger("jarvis.google.tasks")

BASE = "https://tasks.googleapis.com/tasks/v1"
LIST = "@default"  # the account's "My Tasks"
_lock = threading.Lock()
_last_run = [0.0]


def _state_path() -> Path:
    return auth.DATA_DIR / "google_tasks_sync.json"


def _load_state() -> dict[str, Any]:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"links": {}}


def _save_state(state: dict[str, Any]) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    tmp.replace(p)


def _fingerprint(text: str, done: bool, due: str) -> str:
    return hashlib.sha1(f"{' '.join(text.split())}|{int(done)}|{due}".encode()).hexdigest()[:12]


def _local_fp(t: Any) -> str:
    return _fingerprint(t.text, t.done, t.due)


def _google_fp(g: dict[str, Any]) -> str:
    return _fingerprint(g.get("title", ""), g.get("status") == "completed", (g.get("due") or "")[:10])


def _google_body(t: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"title": t.text, "status": "completed" if t.done else "needsAction"}
    body["due"] = f"{t.due}T00:00:00.000Z" if t.due else None
    return body


def _fetch_all() -> list[dict[str, Any]]:
    items, page = [], None
    while True:
        params = {"showCompleted": "true", "showHidden": "true", "maxResults": 100}
        if page:
            params["pageToken"] = page
        resp = api.call("GET", f"{BASE}/lists/{LIST}/tasks", params=params)
        items += resp.get("items", [])
        page = resp.get("nextPageToken")
        if not page:
            return [g for g in items if g.get("title", "").strip() and not g.get("deleted")]


def _ts(rfc3339: str) -> float:
    try:
        return datetime.fromisoformat(rfc3339.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0.0


class SyncRefused(Exception):
    """The sync would do something it shouldn't do on its own; nothing was changed on either side."""


MASS_DELETE = 3  # more than this many deletions, and more than half the links, in one sync is refused


def _check_safe(state: dict[str, Any], links: dict[str, dict[str, Any]], path: Path,
                by_id: dict[str, Any], remote: dict[str, dict[str, Any]]) -> None:
    """2026-10-19: an eval process synced its own temp tasks.md against the user's Google Tasks, and
    the user's eight tasks were deleted at Google. The links belong to one tasks.md; and a sync that
    would delete most of the linked tasks on either side is refused, not run."""
    here = str(path.resolve())
    if links and state.get("tasks_file") and state["tasks_file"] != here:
        raise SyncRefused(f"the Google Tasks links belong to {state['tasks_file']}, not {here}")
    state["tasks_file"] = here
    gone_here = sum(1 for tid, link in links.items() if tid not in by_id and link["g"] in remote)
    gone_there = sum(1 for tid, link in links.items() if tid in by_id and link["g"] not in remote)
    for gone, side in ((gone_here, "Google"), (gone_there, "tasks.md")):
        if gone > MASS_DELETE and gone * 2 > len(links):
            raise SyncRefused(f"{gone} of {len(links)} linked tasks would be deleted from {side} in one sync")


def sync() -> dict[str, int]:
    """One two-way sync; counts of what moved. Raises GoogleNotConnected when not signed in."""
    from app.agent.tools import notes as nm

    with _lock, nm.TASKS_LOCK:
        state = _load_state()
        links: dict[str, dict[str, Any]] = state.setdefault("links", {})
        remote = {g["id"]: g for g in _fetch_all()}
        path, lines = nm._read_tasks()
        local_mtime = path.stat().st_mtime if path.is_file() else 0.0
        items = nm._parse_tasks(lines)
        # A hand-typed line gets its stored id now, and the file is saved below, so the link survives
        # (unsaved, the next read would derive another id and the sync would delete it at Google).
        touched_local = any(not t.stored_id for t in items)
        nm._assign_ids(items)
        by_id = {t.id: t for t in items}
        _check_safe(state, links, path, by_id, remote)
        n = {"to_google": 0, "from_google": 0, "created_here": 0, "created_there": 0,
             "deleted_here": 0, "deleted_there": 0}
        drop_local: set[str] = set()
        linked_remote = {link["g"] for link in links.values()}

        for tid, link in list(links.items()):
            t, g = by_id.get(tid), remote.get(link["g"])
            local_changed = t is not None and _local_fp(t) != link["fp"]
            remote_changed = g is not None and _google_fp(g) != link["fp"]
            if t is None and g is None:
                del links[tid]
            elif t is None:  # deleted here
                if remote_changed:  # ...but edited there since: bring it back
                    t = _add_local(nm, lines, items, g)
                    links.pop(tid)
                    links[t.id] = {"g": g["id"], "fp": _google_fp(g)}
                    n["created_here"] += 1
                    touched_local = True
                else:
                    _delete_remote(link["g"])
                    del links[tid]
                    n["deleted_there"] += 1
            elif g is None:  # deleted at Google
                if local_changed:
                    g = api.call("POST", f"{BASE}/lists/{LIST}/tasks", body=_google_body(t))
                    links[tid] = {"g": g["id"], "fp": _local_fp(t)}
                    n["created_there"] += 1
                else:
                    drop_local.add(tid)
                    del links[tid]
                    n["deleted_here"] += 1
            elif local_changed and (not remote_changed or local_mtime > _ts(g.get("updated", ""))):
                api.call("PATCH", f"{BASE}/lists/{LIST}/tasks/{g['id']}", body=_google_body(t))
                link["fp"] = _local_fp(t)
                n["to_google"] += 1
            elif remote_changed:
                _copy_into(nm, t, g)
                link["fp"] = _local_fp(t)
                n["from_google"] += 1
                touched_local = True

        for t in items:  # new here
            if t.id not in links and t.id not in drop_local:
                g = api.call("POST", f"{BASE}/lists/{LIST}/tasks", body=_google_body(t))
                links[t.id] = {"g": g["id"], "fp": _local_fp(t)}
                n["created_there"] += 1
        for gid, g in remote.items():  # new at Google
            if gid not in linked_remote and gid not in {link["g"] for link in links.values()}:
                t = _add_local(nm, lines, items, g)
                links[t.id] = {"g": gid, "fp": _google_fp(g)}
                n["created_here"] += 1
                touched_local = True

        if drop_local:
            for t in sorted((t for t in items if t.id in drop_local), key=lambda t: -t.index):
                nm._cancel_reminder(t)
                del lines[t.index]
            items = nm._parse_tasks(lines)
            touched_local = True
        if touched_local:
            nm._save_tasks(path, lines, items)
        state["last_sync"] = time.time()
        _save_state(state)
        _last_run[0] = time.time()
        if any(n.values()):
            logger.info("Google Tasks sync: %s", {k: v for k, v in n.items() if v})
        return n


def _done_on(g: dict[str, Any]) -> str:
    """The local day Google says the task was completed; '' (= today when rendered) if it doesn't say."""
    stamp = g.get("completed") or ""
    try:
        return f"{datetime.fromisoformat(stamp.replace('Z', '+00:00')).astimezone():%Y-%m-%d}" if stamp else ""
    except ValueError:
        return ""


def _add_local(nm: Any, lines: list[str], items: list[Any], g: dict[str, Any]) -> Any:
    t = nm._Task(len(lines), "", g.get("status") == "completed", " ".join(g["title"].split()),
                 (g.get("due") or "")[:10], "", nm._new_id(items), "", True)
    t.done_on = _done_on(g) if t.done else ""
    lines.append("")
    items.append(t)
    return t


def _copy_into(nm: Any, t: Any, g: dict[str, Any]) -> None:
    done = g.get("status") == "completed"
    due = (g.get("due") or "")[:10]
    if due != t.due:
        t.due_time = ""  # Google moved the day; a local time no longer applies
    t.text, t.due = " ".join(g["title"].split()), due
    if done and not t.done:
        nm._cancel_reminder(t)
        t.done_on = _done_on(g)
    t.done = done


def _delete_remote(gid: str) -> None:
    import httpx

    try:
        api.call("DELETE", f"{BASE}/lists/{LIST}/tasks/{gid}")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code not in (404, 410):
            raise


def sync_soon() -> None:
    """A sync on a background thread (after Jarvis changed tasks.md); no-op when not signed in."""
    if auth.status()["signed_in"]:
        threading.Thread(target=maybe_sync, args=(0.0,), name="google-tasks-sync", daemon=True).start()


def maybe_sync(max_age_s: float = 60.0) -> Optional[dict[str, int]]:
    """Sync if signed in and the last run is older than max_age_s; None when skipped or offline.
    Never raises: a failed sync leaves tasks.md as it is and is retried next time."""
    if time.time() - _last_run[0] < max_age_s or not auth.status()["signed_in"]:
        return None
    try:
        return sync()
    except auth.GoogleNotConnected as exc:
        logger.info("Google Tasks sync skipped: %s", exc)
    except SyncRefused as exc:
        logger.warning("Google Tasks sync refused, nothing changed: %s", exc)
    except Exception as exc:
        logger.warning("Google Tasks sync failed (will retry): %s", exc)
    _last_run[0] = time.time()
    return None
