import os
from pathlib import Path
from typing import Optional
from app.config import settings
from app.agent.tools.paths import resolve_tool_path

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
        entries = sorted(os.listdir(path))
        if not entries:
            return f"Directory '{directory_path}' is empty."
        
        result_lines = [f"Contents of '{directory_path}':"]
        for entry in entries:
            full_item = path / entry
            marker = "[DIR]" if full_item.is_dir() else "[FILE]"
            result_lines.append(f"  {marker} {entry}")
        
        return "\n".join(result_lines)
    except Exception as e:
        return f"Error listing directory '{directory_path}': {str(e)}"
