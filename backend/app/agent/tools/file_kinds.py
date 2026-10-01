"""
What the file tools skip and how they describe files (tool review, 2026-09-30).

Only real clutter is skipped -- version control, dependency and build-cache folders. The search
tools used to also hide every data/build/dist folder, every dot-folder and every image, archive
or database by extension, so ``find_files("*.png")`` found nothing. Binary files are recognised
by their first bytes instead: a ``.gguf`` or ``.pdf`` was read whole as text before.
"""
import os
from datetime import datetime
from pathlib import Path

CLUTTER_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "env",
    "bin", "obj", ".pytest_cache", ".mypy_cache", ".jarvis", ".idea", ".vs",
})

SNIFF_BYTES = 8192


def is_binary(path: Path) -> bool:
    """NUL bytes, or mostly non-text bytes, in the first few KB."""
    try:
        with open(path, "rb") as f:
            head = f.read(SNIFF_BYTES)
    except OSError:
        return False
    if not head:
        return False
    if b"\x00" in head:
        return True
    text_like = sum(1 for b in head if b in (9, 10, 13) or 32 <= b < 127 or b >= 128)
    return text_like / len(head) < 0.7


def human_size(n: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def file_facts(path: Path) -> str:
    """"12.4 KB, modified 2026-09-30 14:02" -- empty if the file can't be read."""
    try:
        st = path.stat()
    except OSError:
        return ""
    return f"{human_size(st.st_size)}, modified {datetime.fromtimestamp(st.st_mtime):%Y-%m-%d %H:%M}"


def binary_refusal(path: Path, shown_as: str) -> str:
    kind = path.suffix.lstrip(".").upper() or "binary"
    return (f"'{shown_as}' is a {kind} file ({file_facts(path)}), not text, so read_file can't show it. "
            "Tell the user what it is instead of guessing its contents.")


def count_entries(folder: Path, cap: int = 100_000) -> int:
    n = 0
    for _root, dirs, files in os.walk(folder):
        n += len(dirs) + len(files)
        if n >= cap:
            break
    return n


def closest_region(content: str, block: str, context: int = 2) -> str:
    """The lines of ``content`` that look most like ``block``, with a little context -- so a
    near-miss can be fixed from the error instead of re-reading the whole file or artifact."""
    import difflib

    lines, want = content.splitlines(), block.splitlines() or [block]
    if not lines:
        return ""
    width = max(1, len(want))
    best, best_at = -1.0, 0
    matcher = difflib.SequenceMatcher(None, "", "\n".join(want))  # b is cached across windows
    for i in range(max(1, len(lines) - width + 1)):
        matcher.set_seq1("\n".join(lines[i:i + width]))
        if matcher.real_quick_ratio() <= best or matcher.quick_ratio() <= best:
            continue  # upper bounds: can't beat the best so far
        score = matcher.ratio()
        if score > best:
            best, best_at = score, i
    lo, hi = max(0, best_at - context), min(len(lines), best_at + width + context)
    return "\n".join(f"{n + 1}: {lines[n]}" for n in range(lo, hi))
