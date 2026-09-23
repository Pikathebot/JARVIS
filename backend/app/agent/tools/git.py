"""
Git tools for the project workspace, ported from the BaseTool stack in ``app/tools/git_tools.py``
(which was registered but never offered to the model).

Git runs as an argv list rather than through a shell, so a file path or commit message is always
one argument -- nothing in it can be read as another command. Reads are LOW_RISK; commit and
checkout change the working tree and need the user's approval (``permissions.py``).
"""
import logging
import subprocess
from pathlib import Path
from typing import Optional

from app.config import settings

logger = logging.getLogger("jarvis.agent.tools.git")

GIT_TIMEOUT_SECONDS = 30
LOG_MAX_COMMITS = 100


def _resolve_repo(repo_path: Optional[str], workspace_path: Optional[str]) -> Path | str:
    """The directory to run git in (inside the workspace), or an error string for the model."""
    ws_root = Path(workspace_path or settings.workspace_path).resolve()
    raw = str(repo_path or ".").strip() or "."
    p = Path(raw)
    resolved = p.resolve() if p.is_absolute() else (ws_root / p).resolve()
    if resolved != ws_root and ws_root not in resolved.parents:
        return f"Error: Access denied. '{repo_path}' resolves outside the active project workspace boundary ('{ws_root}')."
    if not resolved.is_dir():
        return f"Error: Directory not found at '{repo_path}' within workspace '{ws_root}'."
    return resolved


def _git(args: list[str], cwd: Path) -> tuple[int, str]:
    """Run git; returns (exit code, combined output). Exit code -1 means git could not run."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return -1, "Error: git is not installed or not on PATH."
    except subprocess.TimeoutExpired:
        return -1, f"Error: git {' '.join(args[:1])} timed out ({GIT_TIMEOUT_SECONDS}s limit)."
    output = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    if result.returncode != 0:
        return result.returncode, err or output or f"git exited with code {result.returncode}"
    # Some git commands (checkout, commit hooks) report success on stderr.
    return 0, output or err


def git_status(repo_path: str = ".", workspace_path: Optional[str] = None) -> str:
    """
    Show the current git branch and which files are modified, staged or untracked in the workspace.
    Use when the user asks what changed, what is uncommitted, or which branch they are on.

    Args:
        repo_path: Repository directory relative to the workspace (default '.', the workspace root).
        workspace_path: Optional active project workspace root boundary.
    """
    cwd = _resolve_repo(repo_path, workspace_path)
    if isinstance(cwd, str):
        return cwd
    code, out = _git(["status", "--short", "--branch"], cwd)
    if code != 0:
        return f"Error: git status failed: {out}"
    lines = out.splitlines()
    if len(lines) <= 1:
        return f"{out}\nWorking tree clean. Nothing to commit."
    return out


def git_diff(
    file_path: Optional[str] = None,
    staged: bool = False,
    repo_path: str = ".",
    workspace_path: Optional[str] = None,
) -> str:
    """
    Show the line-by-line git diff of uncommitted changes in the workspace repository.

    Args:
        file_path: Optional file to limit the diff to, relative to the repository.
        staged: True for changes already staged for commit (git diff --staged); False for unstaged changes.
        repo_path: Repository directory relative to the workspace (default '.').
        workspace_path: Optional active project workspace root boundary.
    """
    cwd = _resolve_repo(repo_path, workspace_path)
    if isinstance(cwd, str):
        return cwd
    args = ["diff", "--staged"] if staged else ["diff"]
    if file_path and str(file_path).strip():
        args += ["--", str(file_path).strip()]
    code, out = _git(args, cwd)
    if code != 0:
        return f"Error: git diff failed: {out}"
    return out or ("No staged changes." if staged else "No unstaged changes.")


def git_log(
    limit: int = 10,
    file_path: Optional[str] = None,
    repo_path: str = ".",
    workspace_path: Optional[str] = None,
) -> str:
    """
    Show recent git commit history (hash, date, author, subject) of the workspace repository.

    Args:
        limit: How many commits to show (1-100, default 10).
        file_path: Optional file to show the history of, relative to the repository.
        repo_path: Repository directory relative to the workspace (default '.').
        workspace_path: Optional active project workspace root boundary.
    """
    cwd = _resolve_repo(repo_path, workspace_path)
    if isinstance(cwd, str):
        return cwd
    try:
        n = max(1, min(int(limit), LOG_MAX_COMMITS))
    except (TypeError, ValueError):
        n = 10
    args = ["log", f"-n{n}", "--date=short", "--pretty=format:%h %ad %an: %s"]
    if file_path and str(file_path).strip():
        args += ["--", str(file_path).strip()]
    code, out = _git(args, cwd)
    if code != 0:
        return f"Error: git log failed: {out}"
    return out or "No commits yet."


def git_commit(message: str, repo_path: str = ".", workspace_path: Optional[str] = None) -> str:
    """
    Stage all changes and create a git commit -- only when the user explicitly asks to commit.
    Stages everything (git add -A) in the repository, then commits with the given message.

    Args:
        message: The commit message.
        repo_path: Repository directory relative to the workspace (default '.').
        workspace_path: Optional active project workspace root boundary.
    """
    msg = str(message or "").strip()
    if not msg:
        return "Error: Commit message cannot be empty."
    cwd = _resolve_repo(repo_path, workspace_path)
    if isinstance(cwd, str):
        return cwd
    code, out = _git(["add", "-A"], cwd)
    if code != 0:
        return f"Error: git add failed: {out}"
    code, staged = _git(["diff", "--staged", "--name-only"], cwd)
    if code == 0 and not staged:
        return "Nothing to commit: the working tree is clean."
    logger.info("Committing in '%s': %s", cwd, msg[:80])
    code, out = _git(["commit", "-m", msg], cwd)
    if code != 0:
        return f"Error: git commit failed: {out}"
    return out


def git_checkout(target: str, repo_path: str = ".", workspace_path: Optional[str] = None) -> str:
    """
    Switch git branch, or restore a file (discarding its uncommitted changes) -- only when asked.

    Args:
        target: Branch name to switch to, or file path to restore.
        repo_path: Repository directory relative to the workspace (default '.').
        workspace_path: Optional active project workspace root boundary.
    """
    tgt = str(target or "").strip()
    if not tgt:
        return "Error: Checkout target cannot be empty."
    if tgt.startswith("-"):
        return "Error: Checkout target must be a branch or file, not an option."
    cwd = _resolve_repo(repo_path, workspace_path)
    if isinstance(cwd, str):
        return cwd
    code, out = _git(["checkout", tgt], cwd)
    if code != 0:
        return f"Error: git checkout failed: {out}"
    return out or f"Checked out '{tgt}'."
