import logging
from typing import Optional

from app.agent.tools.file_safety import backup_file, shown
from app.agent.tools.paths import is_inside, resolve_tool_path

logger = logging.getLogger("jarvis.agent.tools.write_file")


def write_file(
    file_path: str,
    content: str,
    overwrite: bool = True,
    workspace_path: Optional[str] = None,
    project_id: Optional[str] = None,
    allow_outside: bool = False,
) -> str:
    """
    Create a file, or replace an existing one, with the given text (in the workspace; replacing a file or writing outside the workspace asks the user first, and the old version is backed up). Fill `reason` with why, in a few words.
    Automatically creates any missing parent directories.

    Args:
        file_path: Relative path of the file to create (e.g. 'scripts/run.py', 'docs/notes.md').
        content: The text/code content to write into the file.
        overwrite: Whether to overwrite the file if it already exists (default True).
        workspace_path: Optional active project workspace root boundary.
        project_id: Optional project identifier for RAG indexing.
    """
    clean_path_str = str(file_path or "").strip()
    if not clean_path_str:
        return "Error: File path cannot be empty."

    path, ws_root, err = resolve_tool_path(clean_path_str, workspace_path, allow_outside, what="Target path")
    if err:
        return err

    if path.exists() and path.is_dir():
        return f"Error: Cannot write to '{file_path}' because it is a directory."

    if path.exists() and not overwrite:
        return f"Error: File '{file_path}' already exists and overwrite is set to False."

    try:
        backup = backup_file(path, ws_root) if path.exists() else None
        path.parent.mkdir(parents=True, exist_ok=True)

        content_str = str(content or "")
        path.write_text(content_str, encoding="utf-8")

        line_count = len(content_str.splitlines()) if content_str else 0
        char_count = len(content_str)
        logger.info("Successfully wrote %d chars (%d lines) to '%s'", char_count, line_count, path)

        if is_inside(path, ws_root):
            from app.agent.tools.workspace_index import reindex
            reindex(path, project_id)

        result = f"Successfully wrote {char_count} characters ({line_count} lines) to '{file_path}'."
        if backup:
            result += f" The previous version is saved as '{shown(backup, ws_root)}'."
        return result
    except Exception as e:
        logger.error("Failed to write file '%s': %s", file_path, e)
        return f"Error writing file '{file_path}': {str(e)}"
