import os
import subprocess
import logging
from pathlib import Path
from typing import Optional
from app.config import settings

logger = logging.getLogger("jarvis.agent.tools.sample_tools")

# Under the pipeline's 180 s cap for execute_command, so the tool times out first and can still
# return what the command printed.
COMMAND_TIMEOUT_SECONDS = 120


def _as_text(stream) -> str:
    if stream is None:
        return ""
    if isinstance(stream, bytes):
        return stream.decode("utf-8", errors="replace")
    return str(stream)


def _windows_shell() -> str:
    """pwsh (PowerShell 7) if it is on PATH, else the built-in Windows PowerShell 5.1."""
    import shutil
    return shutil.which("pwsh") or "powershell"


def execute_command(command: str, workspace_path: Optional[str] = None) -> str:
    """
    Execute a shell command locally in the terminal within the active project workspace directory.
    Use only when terminal commands or CLI execution is requested. Fill `reason` with why, in a few words.

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
            timeout=COMMAND_TIMEOUT_SECONDS,
            cwd=str(ws_root)
        )
        output = result.stdout.strip()
        if result.stderr:
            output += ("\n" if output else "") + f"[Stderr]: {result.stderr.strip()}"
        return output or f"[Process exited with code {result.returncode}]"
    except subprocess.TimeoutExpired as te:
        # A build or test run that overruns has usually printed the useful part already.
        partial = _as_text(te.stdout).strip()
        partial_err = _as_text(te.stderr).strip()
        if partial_err:
            partial += ("\n" if partial else "") + f"[Stderr]: {partial_err}"
        note = f"Error: Command timed out after {COMMAND_TIMEOUT_SECONDS}s and was stopped."
        return f"{note} Output before it stopped:\n{partial}" if partial else note
    except Exception as e:
        logger.error("Error executing command '%s': %s", cmd_clean, e)
        return f"Error executing command '{cmd_clean}': {str(e)}"


def delete_file(
    file_path: str,
    workspace_path: Optional[str] = None,
    project_id: Optional[str] = None,
    allow_outside: bool = False,
) -> str:
    """
    Move a file or folder to the Windows Recycle Bin (restorable from there) -- only when the user asks for it to be deleted. It always asks the user first; fill `reason` with why, in a few words.

    Args:
        file_path: Relative or absolute path of the file or folder to delete.
        workspace_path: Optional active project workspace root boundary.
    """
    from app.agent.tools.file_safety import send_to_recycle_bin
    from app.agent.tools.paths import is_inside, resolve_tool_path

    clean_path_str = str(file_path or "").strip()
    if not clean_path_str:
        return "Error: File path cannot be empty."

    path, ws_root, err = resolve_tool_path(clean_path_str, workspace_path, allow_outside, what="Target path")
    if err:
        return err
    if not path.exists():
        return f"Error: '{file_path}' does not exist."
    if path == ws_root or path in ws_root.parents:
        return "Error: That is the workspace itself (or a folder containing it); it can't be deleted from chat."

    was_dir = path.is_dir()
    files = [f for f in path.rglob("*") if f.is_file()] if was_dir else [path]
    try:
        send_to_recycle_bin(path)
    except Exception as e:
        logger.error("Error deleting '%s': %s", path, e)
        return f"Error deleting '{file_path}': {str(e)}"

    logger.info("Moved '%s' to the Recycle Bin (%d files)", path, len(files))
    from app.agent.tools.workspace_index import unindex
    for f in files:
        if is_inside(f, ws_root):
            unindex(f, project_id)
    if was_dir:
        return f"Moved the folder '{file_path}' ({len(files)} files) to the Recycle Bin."
    return f"Moved '{file_path}' to the Recycle Bin."
