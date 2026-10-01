import os
import re
import logging
from pathlib import Path
from typing import Optional
from app.config import settings
from app.agent.tools.paths import is_inside, resolve_tool_path
from app.agent.tools.file_kinds import CLUTTER_DIRS, file_facts, is_binary

SEARCH_SCAN_CAP = 2000
GREP_MAX_BYTES = 5 * 1024 * 1024
GREP_LINE_CHARS = 200


def _around(line: str, start: int, end: int) -> str:
    """A matched line, cut to ~GREP_LINE_CHARS around the match (minified JS, one-line JSON)."""
    line = line.strip() if len(line) <= GREP_LINE_CHARS else line
    if len(line) <= GREP_LINE_CHARS:
        return line
    mid = (start + end) // 2
    lo = max(0, min(mid - GREP_LINE_CHARS // 2, len(line) - GREP_LINE_CHARS))
    return ("..." if lo else "") + line[lo:lo + GREP_LINE_CHARS] + ("..." if lo + GREP_LINE_CHARS < len(line) else "")

logger = logging.getLogger("jarvis.agent.tools.file_search")

IGNORED_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__",
    ".pytest_cache", ".gemini", "data", "dist", "build"
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
        return f"Error: Search directory '{root_dir}' does not exist."

    found: list[tuple[float, str, str]] = []
    try:
        for root, dirs, files in os.walk(start_path):
            dirs[:] = [d for d in dirs if d not in CLUTTER_DIRS]
            for name, is_dir in [(d, True) for d in dirs] + [(f, False) for f in files]:
                full = Path(root) / name
                rel = os.path.relpath(full, shown_from).replace(os.sep, "/")
                if not (full.match(clean_pattern) or Path(name).match(clean_pattern)):
                    continue
                try:
                    mtime = full.stat().st_mtime
                except OSError:
                    continue
                facts = "folder" if is_dir else file_facts(full)
                found.append((mtime, rel + ("/" if is_dir else ""), facts))
                if len(found) >= SEARCH_SCAN_CAP:
                    break
            if len(found) >= SEARCH_SCAN_CAP:
                break
    except Exception as e:
        logger.error("find_files error: %s", e)
        return f"Error searching files: {str(e)}"

    if not found:
        return f"No files or folders matching pattern '{clean_pattern}' were found in '{root_dir}'."
    found.sort(reverse=True)  # newest first
    lines = [f"### Found {len(found)} match(es) for '{clean_pattern}' (newest first):"]
    lines += [f"- `{rel}` ({facts})" for _, rel, facts in found[:max_results]]
    if len(found) > max_results:
        lines.append(f"\n*(Showing {max_results} of {len(found)}. Use a more specific pattern if needed)*")
    return "\n".join(lines)


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
    skipped = 0

    def search_file(fpath: Path):
        nonlocal total_matches, skipped
        try:
            if fpath.stat().st_size > GREP_MAX_BYTES or is_binary(fpath):
                skipped += 1
                return
            lines = fpath.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            return
        rel_p = os.path.relpath(fpath, shown_from).replace(os.sep, "/")
        for idx, line in enumerate(lines):
            m = regex.search(line)
            if not m:
                continue
            matched_results.append({
                "file": rel_p,
                "line": idx + 1,
                "content": _around(line, m.start(), m.end()),
                "before": _around(lines[idx - 1], 0, 0) if idx > 0 else None,
                "after": _around(lines[idx + 1], 0, 0) if idx + 1 < len(lines) else None,
            })
            total_matches += 1
            if total_matches >= max_matches:
                return

    if target_path.is_file():
        search_file(target_path)
    else:
        for root, dirs, files in os.walk(target_path):
            dirs[:] = [d for d in dirs if d not in CLUTTER_DIRS]
            for fname in files:
                search_file(Path(root) / fname)
                if total_matches >= max_matches:
                    break
            if total_matches >= max_matches:
                break

    if not matched_results:
        extra = f" ({skipped} binary or very large file(s) skipped)" if skipped else ""
        return f"No occurrences of '{clean_pattern}' found in '{path}'{extra}."

    output_lines = [f"### Grep Results for `{clean_pattern}` ({len(matched_results)} match(es)):"]
    current_file = None
    for r in matched_results:
        if r["file"] != current_file:
            current_file = r["file"]
            output_lines.append(f"\n**{current_file}**:")
        if r["before"] is not None:
            output_lines.append(f"    L{r['line'] - 1}: {r['before']}")
        output_lines.append(f"  > L{r['line']}: {r['content']}")
        if r["after"] is not None:
            output_lines.append(f"    L{r['line'] + 1}: {r['after']}")

    if total_matches >= max_matches:
        output_lines.append(f"\n*(Results capped at {max_matches} matches)*")
    if skipped:
        output_lines.append(f"\n*({skipped} binary or very large file(s) were not searched)*")

    return "\n".join(output_lines)
