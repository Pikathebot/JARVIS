"""Logs for Settings -> Developer (PLAN 4.17): the tail of backend.log and the children's output.

``backend.log`` (repo root) is written by the app's BackendHost from uvicorn's stdout/stderr and is
only ever appended to (20 MB by 2026-10-19), so it is read from the end and never whole. The model
and sidecars keep their recent output in memory (``stamp`` gives each line a time). Logs hold chat
text and tool arguments: they are served on 127.0.0.1 only, like the rest of the API.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Iterable, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_LOG = REPO_ROOT / "backend.log"

MAX_LINES = 2000
_SCAN_BYTES = 4 * 1024 * 1024

# The client's 2 s polls and the governor's llama-server probes: most of the file, none of the news.
_NOISE = re.compile(
    r'"GET /(?:health|api/awareness/status|governor/status|api/voice/mode|api/game-mode|api/google/status)\b'
    r"|HTTP Request: GET http://127\.0\.0\.1:\d+/(?:slots|health|v1/models)\b"
)
# llama-server logs every request it serves; the governor polls /slots every few seconds, which
# would fill a child's output ring with nothing else. Dropped as the lines arrive.
POLL_REQUEST = re.compile(r"request: GET /(?:slots|health|v1/models|props)\b")
SIDECAR_RING = 300
_LEVEL_HIT = re.compile(r"\[(?:WARNING|ERROR|CRITICAL)\]|^(?:WARNING|ERROR|CRITICAL):|Traceback \(most recent call last\)")
# A line that starts a new log entry; anything else (a traceback's frames) belongs to the one above.
_NEW_ENTRY = re.compile(r"^(?:\d{4}-\d{2}-\d{2} \d{2}:\d{2}|(?:INFO|WARNING|ERROR|DEBUG|CRITICAL):)")


def stamp(line: str) -> str:
    """A child's output line with the time it arrived (the children don't all stamp their own)."""
    return f"{time.strftime('%H:%M:%S')} {line}"


def tail_file(path: Path, max_bytes: int = _SCAN_BYTES) -> list[str]:
    """The file's last ``max_bytes`` as lines (a partial first line dropped); [] when missing."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            start = max(0, size - max_bytes)
            f.seek(start)
            data = f.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    if start > 0 and lines:
        lines = lines[1:]
    return lines


def filter_lines(lines: Iterable[str], *, quiet: bool = True, level: str = "", search: str = "") -> list[str]:
    """``quiet`` drops the polling noise; ``level="warning"`` keeps warnings and errors with their
    traceback lines; ``search`` keeps lines containing it (any case)."""
    wanted_level = (level or "").lower() in ("warning", "warn", "error")
    needle = (search or "").lower()
    out: list[str] = []
    in_hit = False
    for line in lines:
        if quiet and _NOISE.search(line):
            in_hit = False
            continue
        if wanted_level:
            if _LEVEL_HIT.search(line):
                in_hit = True
            elif _NEW_ENTRY.match(line):
                in_hit = False
            if not in_hit:
                continue
        if needle and needle not in line.lower():
            continue
        out.append(line)
    return out


def _model_lines() -> list[str]:
    from app.agent.runtime_process_manager import get_runtime_process_manager

    pm = get_runtime_process_manager()
    with pm._output_lock:
        return list(pm._recent_output)


def _sidecar_lines() -> list[str]:
    from app.agent.captioner import get_image_captioner
    from app.rag.embedding_sidecar import get_embedding_sidecar
    from app.voice.laya_sidecar import get_laya_sidecar

    merged: list[str] = []
    for label, source in (("captioner", get_image_captioner()), ("embedding", get_embedding_sidecar())):
        with source._output_lock:
            merged += [_label(line, label) for line in source._recent_output]
    merged += [_label(line, "laya") for line in list(get_laya_sidecar()._recent)]
    return sorted(merged, key=lambda line: line[:8])  # by time of day; stable within a sidecar


def _label(line: str, label: str) -> str:
    return f"{line[:8]} [{label}] {line[9:]}" if re.match(r"\d\d:\d\d:\d\d ", line) else f"[{label}] {line}"


def read_logs(source: str, lines: int = 300, *, quiet: bool = True, level: str = "",
              search: str = "") -> dict:
    source = (source or "backend").lower()
    n = max(1, min(int(lines or 300), MAX_LINES))
    path: Optional[str] = None
    if source == "backend":
        raw = tail_file(BACKEND_LOG)
        path = str(BACKEND_LOG)
    elif source == "model":
        raw = _model_lines()
    elif source == "sidecars":
        raw = _sidecar_lines()
    else:
        raise ValueError(f"Unknown log source '{source}'. Expected backend, model or sidecars.")
    kept = filter_lines(raw, quiet=quiet, level=level, search=search)
    return {"source": source, "path": path, "lines": kept[-n:], "total_matched": len(kept)}
