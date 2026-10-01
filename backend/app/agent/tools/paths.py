"""
Where a file tool's path points, and whether it may go there.

Inside the workspace a path is always fine. Outside it, the permission gate asks the user (in a
workspace; System mode lets non-critical paths through), and the tool then has to honour that
approval -- before 2026-10-01 every file tool refused anything outside the workspace even after
the user said yes, so the question was pointless. ``allow_outside`` is set by ``execute_tool``
only for calls the tool loops ran through the gate; a model can't pass it itself.
"""
from pathlib import Path
from typing import Optional

from app.config import settings


def workspace_root(workspace_path: Optional[str]) -> Path:
    return Path(workspace_path or settings.workspace_path).resolve()


def is_inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def resolve_tool_path(
    path_str: str, workspace_path: Optional[str], allow_outside: bool = False, what: str = "Path"
) -> tuple[Optional[Path], Path, str]:
    """(resolved path, workspace root, error). A relative path is taken from the workspace root;
    the error is non-empty when the path is outside the workspace and that wasn't approved."""
    root = workspace_root(workspace_path)
    p = Path(str(path_str or "").strip())
    resolved = p.resolve() if p.is_absolute() else (root / p).resolve()
    if not is_inside(resolved, root) and not allow_outside:
        return None, root, (
            f"Error: Access denied. {what} '{path_str}' resolves outside the active project "
            f"workspace boundary ('{root}')."
        )
    return resolved, root, ""
