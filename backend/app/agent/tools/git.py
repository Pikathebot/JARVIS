"""
Git tools for the project workspace, ported from the BaseTool stack in ``app/tools/git_tools.py``
(which was registered but never offered to the model).

Git runs as an argv list rather than through a shell, so a file path or commit message is always
one argument -- nothing in it can be read as another command. Reads are LOW_RISK; commit, switch,
restore, push and pull change the repository and need the user's approval (``permissions.py``).
"""
import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

from app.config import settings

logger = logging.getLogger("jarvis.agent.tools.git")

GIT_TIMEOUT_SECONDS = 30
NETWORK_TIMEOUT_SECONDS = 120
DIFF_MAX_CHARS = 12000
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


def _git(args: list[str], cwd: Path, timeout: int = GIT_TIMEOUT_SECONDS) -> tuple[int, str]:
    """Run git; returns (exit code, combined output). Exit code -1 means git could not run."""
    # No terminal prompt for credentials: nobody can answer it, and push/pull would hang.
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
    except FileNotFoundError:
        return -1, "Error: git is not installed or not on PATH."
    except subprocess.TimeoutExpired:
        return -1, f"Error: git {' '.join(args[:1])} timed out ({timeout}s limit)."
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
    if not out:
        return "No staged changes." if staged else "No unstaged changes."
    if len(out) > DIFF_MAX_CHARS and not (file_path and str(file_path).strip()):
        # A cut-off diff ends mid-hunk; the summary plus one file at a time reads better.
        _, stat = _git(["diff", "--staged", "--stat"] if staged else ["diff", "--stat"], cwd)
        return (f"The diff is large ({len(out)} characters). Summary:" + chr(10) + stat + chr(10) * 2
                + "Call git_diff with file_path to see one file's changes.")
    return out


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


def git_commit(
    message: str,
    files: Optional[list[str]] = None,
    repo_path: str = ".",
    workspace_path: Optional[str] = None,
) -> str:
    """
    Create a git commit of all changes, or only the listed files -- only when the user explicitly asks to commit.
    With `files`, only those paths are staged and committed (anything else already staged stays
    staged, uncommitted); without it, everything is staged (git add -A) first.

    Args:
        message: The commit message.
        files: Paths (relative to the repository) to commit; omit to commit every change.
        repo_path: Repository directory relative to the workspace (default '.').
        workspace_path: Optional active project workspace root boundary.
    """
    msg = str(message or "").strip()
    if not msg:
        return "Error: Commit message cannot be empty."
    cwd = _resolve_repo(repo_path, workspace_path)
    if isinstance(cwd, str):
        return cwd
    if isinstance(files, str):
        files = [files]
    paths = [str(f).strip() for f in (files or []) if str(f or "").strip()]
    # "--" before the paths: a model-supplied "-A" or "--force" is a file name, never an option.
    code, out = _git(["add", "--", *paths] if paths else ["add", "-A"], cwd)
    if code != 0:
        return f"Error: git add failed: {out}"
    code, staged = _git(["diff", "--staged", "--name-only", "--", *paths], cwd)
    if code == 0 and not staged:
        if paths:
            return "Nothing to commit: those files have no changes."
        return "Nothing to commit: the working tree is clean."
    logger.info("Committing in '%s' (%s): %s", cwd, ", ".join(paths) or "all changes", msg[:80])
    # With paths, commit only them: `git commit -- <paths>` leaves other staged files out.
    code, out = _git(["commit", "-m", msg, "--", *paths] if paths else ["commit", "-m", msg], cwd)
    if code != 0:
        return f"Error: git commit failed: {out}"
    return out


def _current_branch(cwd: Path) -> str:
    code, out = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    return out.strip() if code == 0 else ""


def git_switch(branch: str, create: bool = False, repo_path: str = ".", workspace_path: Optional[str] = None) -> str:
    """
    Switch to another git branch, or create one and switch to it with create=true -- only when the user asks.

    Args:
        branch: Branch name.
        create: Create the branch from the current commit first (git switch -c).
        repo_path: Repository directory relative to the workspace (default '.').
        workspace_path: Optional active project workspace root boundary.
    """
    name = str(branch or "").strip()
    if not name:
        return "Error: Branch name cannot be empty."
    if name.startswith("-"):
        return "Error: Branch name must not start with '-'."
    cwd = _resolve_repo(repo_path, workspace_path)
    if isinstance(cwd, str):
        return cwd
    code, out = _git(["switch", "-c", name] if create else ["switch", name], cwd)
    if code != 0:
        return f"Error: git switch failed: {out}"
    return f"{'Created and switched to' if create else 'Switched to'} branch '{name}'."


def git_restore(file_path: str, repo_path: str = ".", workspace_path: Optional[str] = None) -> str:
    """
    Throw away a file's uncommitted changes, putting it back as it was in the last commit -- only when the user asks; the current version is backed up first.

    Args:
        file_path: File to restore, relative to the repository.
        repo_path: Repository directory relative to the workspace (default '.').
        workspace_path: Optional active project workspace root boundary.
    """
    rel = str(file_path or "").strip()
    if not rel:
        return "Error: File path cannot be empty."
    cwd = _resolve_repo(repo_path, workspace_path)
    if isinstance(cwd, str):
        return cwd
    from app.agent.tools.file_safety import backup_file, shown
    from app.agent.tools.paths import is_inside, workspace_root

    target = (cwd / rel).resolve()
    ws_root = workspace_root(workspace_path)
    if not is_inside(target, ws_root):
        return f"Error: Access denied. '{file_path}' resolves outside the workspace."
    backup = backup_file(target, ws_root)
    # "--" so a model-supplied "-p" or "--staged" is a file name, never an option.
    code, out = _git(["restore", "--", rel], cwd)
    if code != 0:
        return f"Error: git restore failed: {out}"
    note = f" Its uncommitted version is saved as '{shown(backup, ws_root)}'." if backup else ""
    return f"Restored '{rel}' to the last commit.{note}"


def git_push(repo_path: str = ".", workspace_path: Optional[str] = None) -> str:
    """
    Push the current branch to its remote (setting the upstream on first push) -- only when the user asks.

    Args:
        repo_path: Repository directory relative to the workspace (default '.').
        workspace_path: Optional active project workspace root boundary.
    """
    cwd = _resolve_repo(repo_path, workspace_path)
    if isinstance(cwd, str):
        return cwd
    branch = _current_branch(cwd)
    if not branch or branch == "HEAD":
        return "Error: Not on a branch (detached HEAD); nothing to push."
    has_upstream = _git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], cwd)[0] == 0
    args = ["push"] if has_upstream else ["push", "-u", "origin", branch]
    code, out = _git(args, cwd, timeout=NETWORK_TIMEOUT_SECONDS)
    if code != 0:
        return f"Error: git push failed: {out}"
    return out or f"Pushed '{branch}'."


def git_pull(repo_path: str = ".", workspace_path: Optional[str] = None) -> str:
    """
    Fetch and fast-forward the current branch from its remote -- only when the user asks; it never merges, so it can't start a conflict.

    Args:
        repo_path: Repository directory relative to the workspace (default '.').
        workspace_path: Optional active project workspace root boundary.
    """
    cwd = _resolve_repo(repo_path, workspace_path)
    if isinstance(cwd, str):
        return cwd
    code, out = _git(["pull", "--ff-only"], cwd, timeout=NETWORK_TIMEOUT_SECONDS)
    if code != 0:
        low = out.lower()
        if "fast-forward" in low or "diverg" in low:
            return ("Can't fast-forward: this branch and the remote have both changed. Nothing was "
                    "touched. Tell the user; merging or rebasing is their call.")
        return f"Error: git pull failed: {out}"
    return out or "Already up to date."
