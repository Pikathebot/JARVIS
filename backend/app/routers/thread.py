"""The one continuing Freeform thread (PLAN 4.18): its episodes, newest first -- what the client's
sidebar table of contents lists -- and its messages a page at a time for the scroll-back."""
from typing import Any, Optional

from fastapi import APIRouter, Query

from app.memory.thread import THREAD_SESSION_ID

router = APIRouter(prefix="/api/thread", tags=["thread"])


def _store():
    from app.main import memory_store
    return memory_store


@router.get("/episodes")
def list_episodes(limit: int = Query(default=50, ge=1, le=500)) -> dict[str, Any]:
    episodes = _store().list_episodes(THREAD_SESSION_ID, limit=limit)
    for ep in episodes:
        ep.pop("embedding", None)  # a vector is no use to the client
    return {"session_id": THREAD_SESSION_ID, "episodes": episodes}


@router.get("/messages")
def list_messages(before_id: Optional[int] = Query(default=None, ge=1),
                  limit: int = Query(default=80, ge=1, le=500)) -> dict[str, Any]:
    """The newest ``limit`` messages older than ``before_id``, oldest first, with ``id`` and
    ``created_at``; ``has_more`` says whether older ones remain. A row that opens an episode
    carries ``episode_start`` (the episode's id): the episode's first message is the first one
    after its ``start_after_message_id``, which only the server can tell on a page's first row."""
    store = _store()
    messages, more = store.get_message_page(THREAD_SESSION_ID, before_id=before_id, limit=limit)
    if messages:
        starts = sorted(((e["start_after_message_id"], e["id"]) for e in store.list_episodes(THREAD_SESSION_ID)))
        prev = store.previous_message_id(THREAD_SESSION_ID, messages[0]["id"])
        for row in messages:
            for after, episode_id in starts:
                if prev <= after < row["id"]:
                    row["episode_start"] = episode_id  # the newest such episode wins (empty ones skipped)
            prev = row["id"]
    return {"session_id": THREAD_SESSION_ID, "messages": messages, "has_more": more}
