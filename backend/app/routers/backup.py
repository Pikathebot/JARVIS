"""Settings > Backup: one zip of everything Jarvis keeps (PLAN 4.29 B6, ``app/backup.py``).
Not on the phone port (``app.remote.REMOTE_ROUTES`` doesn't list it). Restore is deliberately
not an endpoint: it needs a stopped backend -- ``scripts/backup.py restore``."""
import os
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.backup import BackupError, create_backup, default_locations
from app.config import BASE_DIR

router = APIRouter(prefix="/api/backup", tags=["backup"])


class BackupRequest(BaseModel):
    include_secrets: bool = False


def backups_dir() -> Path:
    return Path(os.environ.get("JARVIS_BACKUP_DIR") or Path(BASE_DIR).parent / "backups")


@router.post("", response_model=dict)
def make_backup(request: BackupRequest) -> dict:
    """Write a backup zip under ``backups/`` and say where it is."""
    dest = backups_dir() / f"jarvis-backup-{datetime.now():%Y%m%d-%H%M%S}.zip"
    try:
        report = create_backup(default_locations(), dest, include_secrets=request.include_secrets)
    except (BackupError, OSError) as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Backup failed: {exc}") from exc
    return {"path": str(report.path), "files": report.files, "bytes": report.bytes_written}
