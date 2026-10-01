import os
import logging
from pathlib import Path
from typing import Optional
from app.config import settings

logger = logging.getLogger("jarvis.agent.tools.patch_file")


def patch_file(
    file_path: str,
    search_block: str,
    replacement_block: str,
    workspace_path: Optional[str] = None,
    project_id: Optional[str] = None
) -> str:
    """
    Perform a precise in-place modification to an existing file on disk inside the project workspace
    by replacing a specific search block with new content.
    Always use this tool when editing or updating existing code, fixing functions, or changing configuration lines without rewriting entire files.

    Args:
        file_path: Relative path of the file to patch (e.g. 'scripts/test_calc.py', 'backend/app/main.py').
        search_block: The exact existing text snippet/block to find and replace.
        replacement_block: The new replacement text snippet/block.
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

    # Fallback resolution inside workspace if not found directly
    if not path.exists():
        candidates = [
            ws_root / "files" / Path(clean_path_str).name,
            ws_root / Path(clean_path_str).name,
        ]
        for cand in candidates:
            cand_res = cand.resolve()
            if (cand_res == ws_root or ws_root in cand_res.parents) and cand_res.exists() and cand_res.is_file():
                path = cand_res
                break

    if not path.exists():
        return f"Error: Cannot patch file '{file_path}' because it does not exist within workspace '{ws_root}'."

    if path.is_dir():
        return f"Error: Cannot patch '{file_path}' because it is a directory."

    if not search_block:
        return "Error: Search block cannot be empty."

    try:
        current_content = path.read_text(encoding="utf-8")
        
        # Standardize line endings for reliable matching
        normalized_current = current_content.replace("\r\n", "\n")
        normalized_search = search_block.replace("\r\n", "\n")
        normalized_replace = (replacement_block or "").replace("\r\n", "\n")

        match_count = normalized_current.count(normalized_search)

        # Fallback: whitespace-stripped matching if exact match failed
        if match_count == 0:
            current_lines = [l.rstrip() for l in normalized_current.splitlines()]
            search_lines = [l.rstrip() for l in normalized_search.splitlines() if l.strip()]
            
            # If search block is 1 line or simple, check if matching line exists
            if len(search_lines) == 1 and search_lines[0] in current_lines:
                idx = current_lines.index(search_lines[0])
                orig_line = normalized_current.splitlines()[idx]
                normalized_search = orig_line
                match_count = normalized_current.count(normalized_search)

        if match_count == 0:
            preview = current_content[:400] + ("..." if len(current_content) > 400 else "")
            return (
                f"Error: The specified search block was not found in '{file_path}'. "
                f"Current file contents:\n```\n{preview}\n```\n"
                "Please use the exact content from the file above as your search block."
            )

        if match_count > 1:
            return (
                f"Error: Found {match_count} occurrences of the search block in '{file_path}'. "
                "The search block must be unique. Please include more surrounding context lines to disambiguate the target location."
            )

        # Exact single replacement
        new_content = normalized_current.replace(normalized_search, normalized_replace, 1)

        # Restore original line ending style if CRLF was present
        if "\r\n" in current_content:
            new_content = new_content.replace("\n", "\r\n")

        path.write_text(new_content, encoding="utf-8")
        from app.agent.tools.workspace_index import reindex
        reindex(path, project_id)

        old_lines = len(normalized_search.splitlines())
        new_lines = len(normalized_replace.splitlines())
        delta = new_lines - old_lines
        sign = f"+{delta}" if delta >= 0 else str(delta)

        logger.info("Successfully patched '%s' (%s lines)", path, sign)
        return f"Successfully patched '{file_path}' (replaced {old_lines} line(s) with {new_lines} line(s), delta: {sign} lines)."
    except Exception as e:
        logger.error("Failed to patch file '%s': %s", file_path, e)
        return f"Error patching file '{file_path}': {str(e)}"
