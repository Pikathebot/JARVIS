"""Back up and restore everything Jarvis keeps (PLAN 4.29 B6).

One zip holds: the chat/project database (taken with SQLite's online backup API, never a copy of
the live file), the repo-root ``data/`` state files, each project's files and artifacts, the
workspace's loose files, and the notes folder. RAG indexes are left out -- they are rebuilt from
the project files. Google sign-in and the push key are left out unless asked: a backup zip tends
to travel to cloud storage, and those are credentials.

Restore is for a stopped backend onto a clean checkout; it refuses to overwrite anything unless
told to, and refuses archive entries that would land outside the target folders.
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

FORMAT_VERSION = 1
MANIFEST = "manifest.json"
DB_ENTRY = "db/jarvis_memory.db"

# Credentials in repo-root data/, only with include_secrets.
SECRET_FILES = frozenset({"google_client_secret.json", "google_token.bin", "remote_vapid.pem"})
# Folders under data/ that are caches, not state.
SKIPPED_DATA_DIRS = frozenset({"webview_cache"})
# Rebuilt from the project files; backing them up would only bloat the zip.
SKIPPED_WORKSPACE_DIRS = frozenset({"indexes"})
SKIPPED_SUFFIXES = (".tmp",)


@dataclass(frozen=True)
class BackupLocations:
    """Where each kind of state lives; tests pass temp folders."""

    db_path: Path
    data_dir: Path
    workspace_dir: Path
    notes_dir: Path


@dataclass(frozen=True)
class BackupReport:
    path: Path
    files: int
    bytes_written: int


class BackupError(Exception):
    pass


def _walk(root: Path, skipped_dirs: frozenset[str]):
    for folder, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in skipped_dirs]
        for name in names:
            if name.endswith(SKIPPED_SUFFIXES):
                continue
            yield Path(folder) / name


def _snapshot_database(db_path: Path, out: Path) -> None:
    with contextlib.closing(sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)) as src, \
            contextlib.closing(sqlite3.connect(out)) as dst:
        src.backup(dst)


def _entries(loc: BackupLocations, include_secrets: bool):
    """(archive name, source path) for every file except the database."""
    if loc.data_dir.is_dir():
        for path in _walk(loc.data_dir, SKIPPED_DATA_DIRS):
            if not include_secrets and path.parent == loc.data_dir and path.name in SECRET_FILES:
                continue
            yield "data/" + path.relative_to(loc.data_dir).as_posix(), path
    if loc.workspace_dir.is_dir():
        for path in _walk(loc.workspace_dir, SKIPPED_WORKSPACE_DIRS):
            yield "workspace/" + path.relative_to(loc.workspace_dir).as_posix(), path
    if loc.notes_dir.is_dir():
        for path in _walk(loc.notes_dir, frozenset()):
            yield "notes/" + path.relative_to(loc.notes_dir).as_posix(), path


def create_backup(loc: BackupLocations, dest: Path, include_secrets: bool = False) -> BackupReport:
    """Write the backup zip to *dest* (a temp file swapped in on success)."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_zip = dest.with_name(dest.name + f".{os.getpid()}.tmp")
    count = 0
    try:
        with tempfile.TemporaryDirectory(prefix="jarvis-backup-") as scratch:
            with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as zf:
                if loc.db_path.is_file():
                    snap = Path(scratch) / "jarvis_memory.db"
                    _snapshot_database(loc.db_path, snap)
                    zf.write(snap, DB_ENTRY)
                    count += 1
                for name, source in _entries(loc, include_secrets):
                    try:
                        zf.write(source, name)
                    except FileNotFoundError:
                        continue  # vanished while we walked (a temp file, a rotated log)
                    count += 1
                zf.writestr(MANIFEST, json.dumps({
                    "format": FORMAT_VERSION,
                    "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "files": count,
                    "includes_secrets": include_secrets,
                }, indent=2))
        os.replace(tmp_zip, dest)
    except BaseException:
        tmp_zip.unlink(missing_ok=True)
        raise
    return BackupReport(path=dest, files=count, bytes_written=dest.stat().st_size)


def _target_for(name: str, loc: BackupLocations) -> Optional[Path]:
    prefix, _, rest = name.partition("/")
    base = {"data": loc.data_dir, "workspace": loc.workspace_dir, "notes": loc.notes_dir}.get(prefix)
    if base is None or not rest:
        return None
    target = (base / rest).resolve()
    root = base.resolve()
    if target != root and root not in target.parents:
        raise BackupError(f"Archive entry escapes its folder: {name}")
    return target


def restore_backup(archive: Path, loc: BackupLocations, overwrite: bool = False) -> int:
    """Unpack *archive* into *loc*; returns the number of files restored. With the backend stopped."""
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
        if MANIFEST not in names:
            raise BackupError("Not a Jarvis backup (no manifest).")
        if json.loads(zf.read(MANIFEST)).get("format") != FORMAT_VERSION:
            raise BackupError("This backup was made by a different version of Jarvis.")

        plan: list[tuple[str, Path]] = []
        for name in names:
            if name == MANIFEST or name.endswith("/"):
                continue
            if name == DB_ENTRY:
                plan.append((name, loc.db_path.resolve()))
                continue
            target = _target_for(name, loc)
            if target is not None:
                plan.append((name, target))

        if not overwrite:
            clash = [str(t) for _, t in plan if t.exists()]
            if clash:
                raise BackupError(
                    f"{len(clash)} file(s) already exist (first: {clash[0]}). Restore onto a clean "
                    "checkout, or pass overwrite."
                )

        for name, target in plan:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + f".{os.getpid()}.tmp")
            try:
                with zf.open(name) as src, open(tmp, "wb") as out:
                    while chunk := src.read(1 << 20):
                        out.write(chunk)
                    out.flush()
                    os.fsync(out.fileno())
                if name == DB_ENTRY:
                    _check_database(tmp)
                    for sidecar in ("-wal", "-shm"):  # a stale journal would replay over the restore
                        Path(str(target) + sidecar).unlink(missing_ok=True)
                os.replace(tmp, target)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
    return len(plan)


def _check_database(path: Path) -> None:
    with contextlib.closing(sqlite3.connect(path)) as db:
        verdict = db.execute("PRAGMA integrity_check").fetchone()
    if not verdict or verdict[0] != "ok":
        raise BackupError("The database in the backup failed its integrity check.")


def default_locations() -> BackupLocations:
    """The live paths, resolved the way the running backend resolves them."""
    from app.agent.tools.notes import notes_dir
    from app.config import BASE_DIR, settings

    url = settings.database_url_resolved
    db = Path(url[len("sqlite:///"):]) if url.startswith("sqlite:///") else Path(settings.memory_db_path)
    return BackupLocations(
        db_path=db,
        data_dir=Path(BASE_DIR).parent / "data",
        workspace_dir=Path(settings.workspace_path).resolve(),
        notes_dir=notes_dir(),
    )
