import os
from pathlib import Path
from typing import Optional
from app.config import settings
from app.agent.tools.paths import resolve_tool_path
from app.agent.tools.file_kinds import CLUTTER_DIRS, count_entries, file_facts

LIST_CAP = 200

def list_directory(directory_path: str = ".", workspace_path: Optional[str] = None, allow_outside: bool = False) -> str:
    """
    List files and folders in a directory (in the workspace; a folder outside it asks the user first).
    Always use this tool when the user asks to list, view, or explore directory contents.

    Args:
        directory_path: Relative directory path within the project (e.g. '.', 'docs', 'backend'). Defaults to '.'.
        workspace_path: Optional active project workspace root boundary.
    """
    raw_path = str(directory_path or ".").strip() or "."
    path, ws_root, err = resolve_tool_path(raw_path, workspace_path, allow_outside, what="Directory")
    if err:
        return err

    if not path.exists():
        return f"Error: Directory not found at '{directory_path}' within workspace '{ws_root}'."
    if not path.is_dir():
        return f"Error: '{directory_path}' is a file, not a directory."
    
    try:
        names = sorted(os.listdir(path), key=str.lower)
    except Exception as e:
        return f"Error listing directory '{directory_path}': {str(e)}"
    if not names:
        return f"Directory '{directory_path}' is empty."

    folders = [n for n in names if (path / n).is_dir()]
    files = [n for n in names if not (path / n).is_dir()]
    lines = [f"Contents of '{directory_path}' ({len(folders)} folders, {len(files)} files):"]
    shown = 0
    for name in folders:
        if shown >= LIST_CAP:
            break
        if name in CLUTTER_DIRS:
            # node_modules, .git, .venv ...: one line, not thousands.
            lines.append(f"  [DIR] {name}/  ({count_entries(path / name):,} entries, not expanded)")
        else:
            lines.append(f"  [DIR] {name}/")
        shown += 1
    for name in files:
        if shown >= LIST_CAP:
            break
        lines.append(f"  [FILE] {name}  ({file_facts(path / name)})")
        shown += 1
    if len(names) > shown:
        lines.append(f"  ...and {len(names) - shown} more; use find_files to search inside.")
    return "\n".join(lines)
