"""Settings > Updates: llama.cpp and the voice packages, checked and installed on request.
Not on the phone port (``app.remote.REMOTE_ROUTES`` doesn't list it)."""
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.updates.manager import UpdateManager, get_update_manager

router = APIRouter(prefix="/api/updates", tags=["updates"])


class StartRequest(BaseModel):
    action: str = Field(default="update", pattern="^(update|rollback)$")


def _manager() -> UpdateManager:
    manager = get_update_manager()
    if manager is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Updates aren't set up in this backend.")
    return manager


@router.get("", response_model=dict)
async def updates_status(check: bool = Query(default=False)) -> dict[str, Any]:
    """Each tool with its installed and newest version. ``check=true`` asks GitHub and PyPI again."""
    return await _manager().status(check=check)


@router.post("/{tool}", response_model=dict)
async def start_update(tool: str, body: StartRequest) -> dict[str, Any]:
    """Starts an update or roll back. Poll ``GET /api/updates/job`` for progress."""
    try:
        job = _manager().start(tool, body.action)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No tool {tool!r}")
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    return {"job": job.to_dict()}


@router.get("/job", response_model=dict)
async def current_job() -> dict[str, Any]:
    job = _manager().job
    return {"job": job.to_dict() if job else None}


@router.delete("/job", response_model=dict)
async def cancel_job() -> dict[str, Any]:
    """Stops a llama.cpp download; once files are being swapped the job finishes."""
    return {"cancelled": _manager().cancel()}


@router.delete("/{tool}/pending", response_model=dict)
async def cancel_pending(tool: str) -> dict[str, Any]:
    """Drops an install queued for the next start."""
    return {"cancelled": _manager().cancel_pending(tool)}
