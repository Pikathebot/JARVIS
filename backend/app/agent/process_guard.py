"""
Tie child processes to the backend's lifetime.

``llama-server`` children used to outlive the backend whenever it did not shut down cleanly
(a crash, a ``Stop-Process -Force``, the launcher being killed): the next backend found a
healthy server on the port, adopted it as "externally managed", and served whatever weights
it happened to have -- the classic "I selected a model and nothing changed" symptom.

On Windows a Job Object with ``KILL_ON_JOB_CLOSE`` makes the OS do the cleanup: every
process assigned to the job dies when the last handle to the job closes, which happens when
this process exits, for any reason. Elsewhere this is a no-op; POSIX callers rely on the
process group.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger("jarvis.agent.process_guard")

_job: Optional[Any] = None
_job_failed = False


def _get_job() -> Optional[Any]:
    """The single job for this backend, created on first use. None when unavailable."""
    global _job, _job_failed
    if _job is not None or _job_failed or os.name != "nt":
        return _job
    try:
        import win32job  # type: ignore

        job = win32job.CreateJobObject(None, "")
        info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
        _job = job
    except Exception as e:
        _job_failed = True
        logger.warning("Job object unavailable; child processes will not be tied to the backend: %s", e)
    return _job


def tie_to_backend(pid: int) -> bool:
    """
    Make ``pid`` die with this process. Returns False when that could not be arranged, in
    which case the child is an ordinary detached process and the port sweeps remain the
    fallback.
    """
    job = _get_job()
    if job is None:
        return False
    try:
        import win32api  # type: ignore
        import win32con  # type: ignore
        import win32job  # type: ignore

        handle = win32api.OpenProcess(win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, pid)
        try:
            win32job.AssignProcessToJobObject(job, handle)
        finally:
            win32api.CloseHandle(handle)
        return True
    except Exception as e:
        # Typically: this process is itself inside a job that forbids nesting (pre-Windows 8
        # semantics, some launchers). Not fatal.
        logger.warning("Could not tie pid %s to the backend's job object: %s", pid, e)
        return False
