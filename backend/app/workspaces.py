"""
Where a workspace's files live (PLAN 4.10).

A workspace is either *internal* -- a folder Jarvis made for it, ``<workspace_path setting>/
projects/{id}/`` -- or *external*: a real folder the user picked, which IS the workspace. Jarvis
reads, indexes and writes files in an external folder, but its own bookkeeping (the vector
index, artifact history, memory) always stays in the internal project directory, and nothing of
the user's folder is ever deleted with the workspace.

Uploaded attachments need a home inside the workspace boundary so the file tools can read them
without an approval: ``files/`` in an internal workspace (as before), ``.jarvis/attachments/``
in an external one -- a hidden folder, like an editor's ``.vscode/``, rather than a stray
``files/`` among the user's own.
"""

from pathlib import Path
from typing import Optional

from app.config import settings

EXTERNAL_ATTACHMENTS = Path(".jarvis") / "attachments"


def workspaces_root() -> Path:
    return Path(settings.workspace_path).resolve()


def jarvis_project_dir(project_id: str) -> Path:
    """Jarvis's own directory for a project: indexes, artifacts, memory (and, for an internal
    workspace, the workspace itself)."""
    return workspaces_root() / "projects" / project_id


def is_external(workspace_path: Optional[str], project_id: str) -> bool:
    """True when the workspace is a folder the user picked rather than Jarvis's own."""
    if not workspace_path:
        return False
    try:
        return Path(workspace_path).resolve() != jarvis_project_dir(project_id)
    except OSError:
        return False


def attachments_dir(workspace_path: Optional[str], project_id: str) -> Path:
    """Where uploads to this workspace go (created by the caller)."""
    if is_external(workspace_path, project_id):
        return Path(workspace_path).resolve() / EXTERNAL_ATTACHMENTS
    return jarvis_project_dir(project_id) / "files"


def validate_workspace_folder(raw: str) -> Path:
    """A picked workspace folder: an absolute path to an existing directory. Raises ValueError."""
    path = Path(raw.strip())
    if not path.is_absolute():
        raise ValueError(f"Workspace location must be an absolute path: {raw!r}")
    resolved = path.resolve()
    if not resolved.is_dir():
        raise ValueError(f"Workspace location is not an existing folder: {resolved}")
    return resolved
