import os
import subprocess
import logging
from pathlib import Path
from typing import Optional
from app.config import settings

logger = logging.getLogger("jarvis.agent.tools.sample_tools")


def _windows_shell() -> str:
    """pwsh (PowerShell 7) if it is on PATH, else the built-in Windows PowerShell 5.1."""
    import shutil
    return shutil.which("pwsh") or "powershell"


def execute_command(command: str, workspace_path: Optional[str] = None) -> str:
    """
    Execute a shell command locally in the terminal within the active project workspace directory.
    Use only when terminal commands or CLI execution is requested.

    Args:
        command: The shell command string to execute (e.g. 'git status', 'python --version').
        workspace_path: Optional active project workspace root boundary.
    """
    cmd_clean = str(command or "").strip()
    if not cmd_clean:
        return "Error: Command cannot be empty."

    ws_root = Path(workspace_path or settings.workspace_path).resolve()
    if not ws_root.exists():
        ws_root.mkdir(parents=True, exist_ok=True)

    try:
        # Models write `echo a && echo b`; Windows PowerShell 5.1 rejects `&&`/`||`, PowerShell 7
        # (pwsh) accepts them, so prefer it when installed. The command goes as one argv element,
        # not interpolated into a quoted string through the shell, so embedded quotes survive.
        if os.name == "nt":
            cmd_to_run: list[str] | str = [_windows_shell(), "-NoProfile", "-NonInteractive", "-Command", cmd_clean]
            use_shell = False
        else:
            cmd_to_run = cmd_clean
            use_shell = True
        logger.info("Executing terminal command: '%s' in cwd='%s'", cmd_clean, ws_root)
        result = subprocess.run(
            cmd_to_run,
            shell=use_shell,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(ws_root)
        )
        output = result.stdout.strip()
        if result.stderr:
            output += ("\n" if output else "") + f"[Stderr]: {result.stderr.strip()}"
        return output or f"[Process exited with code {result.returncode}]"
    except subprocess.TimeoutExpired:
        return "Error: Command execution timed out (30s limit)."
    except Exception as e:
        logger.error("Error executing command '%s': %s", cmd_clean, e)
        return f"Error executing command '{cmd_clean}': {str(e)}"


def delete_file(file_path: str, workspace_path: Optional[str] = None) -> str:
    """
    Permanently delete or remove a file from disk within the active project workspace.
    Use ONLY when the user explicitly requests deleting or removing a file.

    Args:
        file_path: Relative or absolute path of the file to delete.
        workspace_path: Optional active project workspace root boundary.
    """
    clean_path_str = str(file_path or "").strip()
    if not clean_path_str:
        return "Error: File path cannot be empty."

    ws_root = Path(workspace_path or settings.workspace_path).resolve()

    p = Path(clean_path_str)
    if p.is_absolute():
        resolved_path = p.resolve()
    else:
        resolved_path = (ws_root / p).resolve()

    # Anti-traversal security check: ensure path is inside active workspace boundary
    if resolved_path != ws_root and ws_root not in resolved_path.parents:
        return f"Error: Access denied. Target path '{file_path}' resolves outside the active project workspace boundary ('{ws_root}')."

    path = resolved_path
    
    if not path.exists():
        return f"Error: File '{file_path}' does not exist within workspace '{ws_root}'."
    if path.is_dir():
        return f"Error: '{file_path}' is a directory, not a file."
    
    try:
        os.remove(path)
        logger.info("Successfully deleted file '%s'", path)
        return f"Successfully deleted file '{file_path}'."
    except Exception as e:
        logger.error("Error deleting file '%s': %s", path, e)
        return f"Error deleting file '{file_path}': {str(e)}"
