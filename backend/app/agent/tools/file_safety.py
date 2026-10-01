"""
Undo for the model's file changes: a copy of a file's previous version before it is overwritten
or patched, and the Windows Recycle Bin instead of a hard delete (tool review, 2026-09-30).

Backups live in ``<workspace>/.jarvis/backups/``; the indexer and the search tools skip dot
folders, so they never show up in RAG or find_files. The newest ``KEEP_PER_FILE`` copies of
each file are kept.
"""
import logging
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.agent.tools.paths import is_inside

logger = logging.getLogger("jarvis.agent.tools.file_safety")

KEEP_PER_FILE = 10


def backup_dir(ws_root: Path) -> Path:
    return ws_root / ".jarvis" / "backups"


def backup_file(path: Path, ws_root: Path) -> Optional[Path]:
    """Copy ``path`` aside before it changes. Returns the copy, or None if there was nothing to
    back up (or the copy failed -- that never blocks the edit, it is only logged)."""
    try:
        if not path.is_file():
            return None
        # Inside the workspace: mirror its relative path; outside: drive and folders flattened.
        rel = path.relative_to(ws_root) if is_inside(path, ws_root) else Path("_outside", *[
            part.replace(":", "") for part in path.parts if part not in ("\\", "/")
        ])
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        target = backup_dir(ws_root) / rel.parent / f"{rel.name}.{stamp}.bak"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        old = sorted(target.parent.glob(f"{rel.name}.*.bak"))
        for stale in old[:-KEEP_PER_FILE]:
            stale.unlink(missing_ok=True)
        return target
    except Exception as e:
        logger.warning("Backup of %s failed: %s", path, e)
        return None


def shown(path: Path, ws_root: Path) -> str:
    return os.path.relpath(path, ws_root).replace(os.sep, "/") if is_inside(path, ws_root) else str(path)


def send_to_recycle_bin(path: Path) -> None:
    """Move a file or folder to the Recycle Bin (restorable from there). Raises OSError."""
    if sys.platform != "win32":
        raise OSError("The Recycle Bin is only available on Windows.")
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    FO_DELETE = 3
    FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 0x4, 0x10, 0x40, 0x400
    op = SHFILEOPSTRUCTW(
        hwnd=None, wFunc=FO_DELETE,
        pFrom=str(path.resolve()) + "\0",  # double-NUL-terminated list of one path
        pTo=None, fFlags=FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI,
    )
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if result != 0 or op.fAnyOperationsAborted or path.exists():
        raise OSError(f"Windows could not move it to the Recycle Bin (code {result}).")
