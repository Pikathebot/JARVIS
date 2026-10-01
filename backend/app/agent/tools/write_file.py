import os
import logging
from pathlib import Path
from typing import Optional
from app.config import settings

logger = logging.getLogger("jarvis.agent.tools.write_file")


def write_file(
    file_path: str,
    content: str,
    overwrite: bool = True,
    workspace_path: Optional[str] = None,
    project_id: Optional[str] = None
) -> str:
    """
    Create or overwrite a file on disk with the provided text content inside the project workspace.
    Automatically creates any missing parent directories. Always use this tool when the user asks to create, write, or generate a new file or script.

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

    ws_root = Path(workspace_path or settings.workspace_path).resolve()

    # Resolve target path relative to workspace root if not absolute
    p = Path(clean_path_str)
    if p.is_absolute():
        resolved_path = p.resolve()
    else:
        resolved_path = (ws_root / p).resolve()

    # Anti-traversal security check: ensure path is inside active workspace boundary
    if resolved_path != ws_root and ws_root not in resolved_path.parents:
        return f"Error: Access denied. Target path '{file_path}' resolves outside the active project workspace boundary ('{ws_root}')."

    path = resolved_path

    if path.exists() and path.is_dir():
        return f"Error: Cannot write to '{file_path}' because it is a directory."

    if path.exists() and not overwrite:
        return f"Error: File '{file_path}' already exists and overwrite is set to False."

    try:
        # Automatically ensure parent directory exists inside workspace
        if path.parent and not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)

        content_str = str(content or "")
        path.write_text(content_str, encoding="utf-8")
        
        line_count = len(content_str.splitlines()) if content_str else 0
        char_count = len(content_str)
        
        logger.info("Successfully wrote %d chars (%d lines) to '%s'", char_count, line_count, path)

        from app.agent.tools.workspace_index import reindex
        reindex(path, project_id)

        return f"Successfully wrote {char_count} characters ({line_count} lines) to '{file_path}'."
    except Exception as e:
        logger.error("Failed to write file '%s': %s", file_path, e)
        return f"Error writing file '{file_path}': {str(e)}"
