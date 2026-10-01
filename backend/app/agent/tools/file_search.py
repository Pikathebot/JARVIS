import os
import re
import logging
from pathlib import Path
from typing import Optional
from app.config import settings
from app.agent.tools.paths import is_inside, resolve_tool_path

logger = logging.getLogger("jarvis.agent.tools.file_search")

IGNORED_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__",
    ".pytest_cache", ".gemini", "data", "dist", "build"
}

IGNORED_EXTENSIONS = {
    ".pyc", ".pyd", ".dll", ".exe", ".so", ".dylib",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg",
    ".zip", ".tar", ".gz", ".7z", ".mp3", ".wav", ".webm",
    ".db", ".sqlite", ".sqlite3"
}


def find_files(
    pattern: str,
    root_dir: str = ".",
    max_results: int = 50,
    workspace_path: Optional[str] = None,
    allow_outside: bool = False
) -> str:
    """
    Search for files and directories matching a glob wildcard pattern across the workspace (a folder outside it asks the user first).
    Always use this tool to locate files by name or extension (e.g. '*.py', '*config*', 'test_*.py').

    Args:
        pattern: Glob pattern to match against file names (e.g. '*.py', '*main*', '*.json').
        root_dir: Root directory path to start searching from (default '.').
        max_results: Maximum number of matched file paths to return (default 50).
        workspace_path: Optional active project workspace root boundary.
    """
    clean_pattern = str(pattern or "").strip()
    if not clean_pattern:
        return "Error: Search pattern cannot be empty."

    clean_root = str(root_dir or ".").strip() or "."
    start_path, ws_root, err = resolve_tool_path(clean_root, workspace_path, allow_outside, what="Search directory")
    if err:
        return err
    shown_from = ws_root if is_inside(start_path, ws_root) else start_path

    if not start_path.exists():
        return f"Error: Search directory '{root_dir}' does not exist within workspace '{ws_root}'."

    matches = []
    try:
        for root, dirs, files in os.walk(start_path):
            # Prune ignored directories in-place
            dirs[:] = [d for d in dirs if d not in IGNORED_DIRS and not d.startswith(".")]

            for fname in files:
                ext = Path(fname).suffix.lower()
                if ext in IGNORED_EXTENSIONS:
                    continue

                full_file_path = Path(root) / fname
                rel_path = os.path.relpath(full_file_path, shown_from).replace("\\", "/")

                if full_file_path.match(clean_pattern) or Path(fname).match(clean_pattern):
                    matches.append(rel_path)
                    if len(matches) >= max_results:
                        break
            if len(matches) >= max_results:
                break

        if not matches:
            return f"No files matching pattern '{clean_pattern}' were found in '{root_dir}'."

        lines = [f"### Found {len(matches)} file(s) matching '{clean_pattern}':"]
        for m in matches:
            lines.append(f"- `{m}`")

        if len(matches) >= max_results:
            lines.append(f"\n*(Results capped at {max_results} files. Use a more specific pattern if needed)*")

        return "\n".join(lines)
    except Exception as e:
        logger.error("find_files error: %s", e)
        return f"Error searching files: {str(e)}"


def grep_in_files(
    pattern: str,
    path: str = ".",
    max_matches: int = 50,
    case_sensitive: bool = False,
    workspace_path: Optional[str] = None,
    allow_outside: bool = False
) -> str:
    """
    Search for a text string or regular expression inside workspace files (a path outside the workspace asks the user first).
    Always use this tool to find where functions, classes, variables, imports, or keywords are defined or used across the codebase.

    Args:
        pattern: The text string or regular expression to search for (e.g. 'class AgentOrchestrator', 'def fetch_url').
        path: File or directory path to search within (default '.').
        max_matches: Maximum number of matching lines to return (default 50).
        case_sensitive: Whether to perform a case-sensitive search (default False).
        workspace_path: Optional active project workspace root boundary.
    """
    clean_pattern = str(pattern or "").strip()
    if not clean_pattern:
        return "Error: Grep search pattern cannot be empty."

    clean_path = str(path or ".").strip() or "."
    target_path, ws_root, err = resolve_tool_path(clean_path, workspace_path, allow_outside, what="Target path")
    if err:
        return err
    shown_from = ws_root if is_inside(target_path, ws_root) else (target_path if target_path.is_dir() else target_path.parent)

    if not target_path.exists():
        return f"Error: Target path '{path}' does not exist within workspace '{ws_root}'."

    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        regex = re.compile(clean_pattern, flags)
    except re.error as e:
        # Fallback to literal search if pattern has unescaped regex special chars
        regex = re.compile(re.escape(clean_pattern), flags)

    matched_results = []
    total_matches = 0

    def search_file(fpath: Path):
        nonlocal total_matches
        try:
            content = fpath.read_text(encoding="utf-8", errors="ignore")
            for line_idx, line in enumerate(content.splitlines(), start=1):
                if regex.search(line):
                    rel_p = os.path.relpath(fpath, shown_from).replace("\\", "/")
                    matched_results.append({
                        "file": rel_p,
                        "line": line_idx,
                        "content": line.strip()
                    })
                    total_matches += 1
                    if total_matches >= max_matches:
                        return
        except Exception:
            pass

    if target_path.is_file():
        search_file(target_path)
    else:
        for root, dirs, files in os.walk(target_path):
            dirs[:] = [d for d in dirs if d not in IGNORED_DIRS and not d.startswith(".")]
            for fname in files:
                ext = Path(fname).suffix.lower()
                if ext in IGNORED_EXTENSIONS:
                    continue
                search_file(Path(root) / fname)
                if total_matches >= max_matches:
                    break
            if total_matches >= max_matches:
                break

    if not matched_results:
        return f"No occurrences of '{clean_pattern}' found in '{path}'."

    output_lines = [f"### Grep Results for `{clean_pattern}` ({len(matched_results)} match(es)):"]
    current_file = None
    for r in matched_results:
        if r["file"] != current_file:
            current_file = r["file"]
            output_lines.append(f"\n**{current_file}**:")
        output_lines.append(f"  - `L{r['line']}`: {r['content']}")

    if total_matches >= max_matches:
        output_lines.append(f"\n*(Results capped at {max_matches} matches)*")

    return "\n".join(output_lines)
