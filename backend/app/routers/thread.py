"""The one continuing Freeform thread (PLAN 4.18): its episodes, newest first -- what the client's
sidebar table of contents lists, and a way to check the episode boundaries from outside."""
from typing import Any

from fastapi import APIRouter, Query

from app.memory.thread import THREAD_SESSION_ID

router = APIRouter(prefix="/api/thread", tags=["thread"])


def _store():
    from app.main import memory_store
    return memory_store


@router.get("/episodes")
def list_episodes(limit: int = Query(default=50, ge=1, le=500)) -> dict[str, Any]:
    episodes = _store().list_episodes(THREAD_SESSION_ID, limit=limit)
    return {"session_id": THREAD_SESSION_ID, "episodes": episodes}
