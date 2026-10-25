"""
The model's hand on a project's PROJECT.md (``app.project_state``, PLAN 4.18): one section edit
per call. The chat's own copy in the head doesn't change -- the edit shows from the next chat.
"""
import logging
from typing import Callable, Optional

from sqlmodel import Session

from app import project_state as state
from app.database.models import Project
from app.database.session import SessionLocal

logger = logging.getLogger("jarvis.agent.tools.project_state")

# Overridable in tests; the app's own database otherwise.
session_factory: Callable[[], Session] = SessionLocal


def _project_name(project_id: str) -> str:
    try:
        with session_factory() as db:
            proj = db.get(Project, project_id)
            return (proj.name if proj else "") or "Project"
    except Exception:
        return "Project"


def project_state(
    action: str,
    text: str,
    workspace_path: Optional[str] = None,
    project_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Update this project's PROJECT.md as its state changes: current work, next steps, what got done, decisions.
    """
    if not project_id:
        return "Error: this conversation has no project, so there is no PROJECT.md."
    if session_id:
        from app.memory.ephemeral import ephemeral_registry
        if ephemeral_registry.get(session_id) is not None:
            return "Not saved: this is an off-the-record conversation, and nothing from it is kept."
    try:
        return state.edit(workspace_path, project_id, _project_name(project_id), action, text)
    except OSError as e:
        logger.warning("PROJECT.md edit failed for %s: %s", project_id, e)
        return f"Error: PROJECT.md could not be written ({e})."
