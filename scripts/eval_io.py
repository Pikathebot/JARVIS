"""Reading and stamping eval result files (stdlib only, so the review page can use it too).

A result file is ``{"meta": {...}, "results": [...]}``; files written before 2026-10-18 are a bare
list of results and load with empty meta. ``meta`` records what produced the run (PLAN 4.15 #2,
after harness2's run marks): the model file, a digest of the exact prompt prefix the model saw
(head system message + tool schemas), of the eval scripts, the git commit and whether the tree was
dirty, sampling and flags. Two files are comparable when their digests differ only by the change
being tested; ``compare_meta`` says what differs.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

# Keys whose difference makes two runs measure different things (shown first by compare_meta).
_PRODUCER_KEYS = ("model_file", "model_bytes", "head_sha", "tools_sha", "rules_override", "skills_override", "tools_dropped",
                  "nets_off", "pre_research", "slot", "max_rounds", "max_tokens", "sampling", "scripts_sha")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def file_sha(*paths: Path) -> str:
    h = hashlib.sha256()
    for p in paths:
        try:
            h.update(Path(p).read_bytes())
        except OSError:
            h.update(b"<missing>")
    return h.hexdigest()[:16]


def git_state() -> dict[str, Any]:
    def run(*args: str) -> str:
        try:
            return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""
    dirty = [line[3:] for line in run("status", "--porcelain", "--untracked-files=no").splitlines() if line.strip()]
    return {"commit": run("rev-parse", "--short", "HEAD"), "dirty_files": len(dirty),
            "dirty_backend": sorted(f for f in dirty if f.startswith(("backend/app/", "skills/", "scripts/eval")))}


def load_results(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, list):
        return {}, data
    return dict(data.get("meta") or {}), list(data.get("results") or [])


def write_results(path: Path, meta: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    Path(path).write_text(json.dumps({"meta": meta, "results": rows}, indent=2, ensure_ascii=False), encoding="utf-8")


def compare_meta(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    """Lines naming what differs between two runs' producers; empty when they match or either is unstamped."""
    if not a or not b:
        return ["(one file has no meta: written before run stamps, comparability unknown)"] if (a or b) else []
    out = []
    for key in _PRODUCER_KEYS:
        if a.get(key) != b.get(key):
            out.append(f"{key}: {a.get(key)!r} vs {b.get(key)!r}")
    ga, gb = a.get("git") or {}, b.get("git") or {}
    if ga.get("commit") != gb.get("commit") or ga.get("dirty_backend") != gb.get("dirty_backend"):
        out.append(f"git: {ga.get('commit')}{'+dirty' if ga.get('dirty_backend') else ''} vs "
                   f"{gb.get('commit')}{'+dirty' if gb.get('dirty_backend') else ''}")
    return out
