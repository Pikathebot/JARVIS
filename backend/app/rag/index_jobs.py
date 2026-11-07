"""Background (re)index runs, one per project, with progress the project page polls (PLAN 4.18).

A Reindex used to be one blocking HTTP call: the app gave up after 30 s while the backend kept
going, and pressing the button again (or switching workspace, which also indexes) started a second
pass over the same files. Now POST starts a job, or returns the one already running, and GET
reports where it is."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger("jarvis.rag.index_jobs")


@dataclass
class IndexJob:
    project_id: str
    state: str = "running"  # running | done | error
    total_files: int = 0
    processed_files: int = 0
    indexed_files: int = 0
    total_chunks: int = 0
    error: Optional[str] = None
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None


_jobs: dict[str, IndexJob] = {}
_lock = threading.Lock()


def status(project_id: str) -> dict[str, Any]:
    """The project's latest job (running or finished since this process started), else idle."""
    with _lock:
        job = _jobs.get(project_id)
        return asdict(job) if job else asdict(IndexJob(project_id=project_id, state="idle", started_at=0.0))


def start(project_id: str, run: Callable[[Callable[[int, int], None]], dict[str, Any]]) -> dict[str, Any]:
    """Start ``run(progress)`` on a thread unless the project is already indexing; either way
    return the job's status. ``run`` returns the indexer's summary dict."""
    with _lock:
        existing = _jobs.get(project_id)
        if existing is not None and existing.state == "running":
            return asdict(existing)
        job = IndexJob(project_id=project_id)
        _jobs[project_id] = job

    def progress(done: int, total: int) -> None:
        job.processed_files, job.total_files = done, total

    def _work() -> None:
        try:
            summary = run(progress)
            job.indexed_files = int(summary.get("indexed_files", 0))
            job.total_chunks = int(summary.get("total_chunks", 0))
            job.state = "done"
        except Exception as exc:  # shown on the page; the thread must not die silently
            logger.warning("Index of project %s failed: %s", project_id, exc)
            job.error = str(exc) or exc.__class__.__name__
            job.state = "error"
        finally:
            job.finished_at = time.time()

    try:
        threading.Thread(target=_work, name=f"index-{project_id[:8]}", daemon=True).start()
    except Exception:  # a job that never started would block every later Reindex
        with _lock:
            _jobs.pop(project_id, None)
        raise
    return asdict(job)
