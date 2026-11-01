"""
Keep the project's RAG index in step with file tools. Without it, workspace search answered
from a file's pre-patch text, and a deleted file stayed searchable (tool review, 2026-09-30).
Best effort: an indexing failure never fails the edit itself.
"""
import logging
import json
from pathlib import Path
from typing import Optional

logger = logging.getLogger("jarvis.agent.tools.workspace_index")


def _project_for(project_id: Optional[str], path: Path) -> Optional[str]:
    """The project whose index ``path`` belongs to. With none given, the active project -- but
    only when the file is inside its workspace or one of its local folders: a Freeform write to
    ~/testing.txt was indexed into whichever project happened to be active (2026-11-01)."""
    if project_id:
        return project_id
    from app.database.session import SessionLocal
    from app.database.models import Project
    from app.workspaces import jarvis_project_dir
    from sqlmodel import select
    with SessionLocal() as db:
        act = db.exec(select(Project).where(Project.is_active == True)).first()  # noqa: E712
        if act is None:
            return None
        roots = [Path(act.workspace_path) if act.workspace_path else jarvis_project_dir(act.id)]
        try:
            roots += [Path(f) for f in json.loads(act.local_folders_json or "[]") if f]
        except (TypeError, ValueError):
            pass
    target = path.resolve()
    return act.id if any(target == r.resolve() or r.resolve() in target.parents for r in roots) else None


def reindex(path: Path, project_id: Optional[str]) -> None:
    """After a file was written or patched."""
    try:
        pid = _project_for(project_id, path)
        if pid:
            from app.rag.indexer import WorkspaceIndexer
            WorkspaceIndexer().index_file(path, project_id=pid)
    except Exception as e:
        logger.debug("Re-indexing %s skipped/failed: %s", path, e)


def unindex(path: Path, project_id: Optional[str]) -> None:
    """After a file was deleted."""
    try:
        pid = _project_for(project_id, path)
        if pid:
            from app.rag.indexer import WorkspaceIndexer
            WorkspaceIndexer().remove_file(path, project_id=pid)
    except Exception as e:
        logger.debug("Un-indexing %s skipped/failed: %s", path, e)
