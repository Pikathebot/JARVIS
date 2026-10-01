"""
Keep the project's RAG index in step with file tools. Without it, workspace search answered
from a file's pre-patch text, and a deleted file stayed searchable (tool review, 2026-09-30).
Best effort: an indexing failure never fails the edit itself.
"""
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("jarvis.agent.tools.workspace_index")


def _project_for(project_id: Optional[str]) -> Optional[str]:
    if project_id:
        return project_id
    from app.database.session import SessionLocal
    from app.database.models import Project
    from sqlmodel import select
    with SessionLocal() as db:
        act = db.exec(select(Project).where(Project.is_active == True)).first()  # noqa: E712
        return act.id if act else None


def reindex(path: Path, project_id: Optional[str]) -> None:
    """After a file was written or patched."""
    try:
        pid = _project_for(project_id)
        if pid:
            from app.rag.indexer import WorkspaceIndexer
            WorkspaceIndexer().index_file(path, project_id=pid)
    except Exception as e:
        logger.debug("Re-indexing %s skipped/failed: %s", path, e)


def unindex(path: Path, project_id: Optional[str]) -> None:
    """After a file was deleted."""
    try:
        pid = _project_for(project_id)
        if pid:
            from app.rag.indexer import WorkspaceIndexer
            WorkspaceIndexer().remove_file(path, project_id=pid)
    except Exception as e:
        logger.debug("Un-indexing %s skipped/failed: %s", path, e)
