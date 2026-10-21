"""The one continuing Freeform thread (PLAN 4.18): its episodes, newest first -- what the client's
sidebar table of contents lists -- and its messages a page at a time for the scroll-back."""
import asyncio
import json
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.memory.thread import THREAD_SESSION_ID, forget_episodes
from app.thread_live import get_thread_live

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


@router.get("/head")
def head() -> dict[str, Any]:
    """The thread's newest message id (0 when empty). Clients check it every few seconds while
    idle and pick up turns another device added -- the phone, the desktop, the HUD (PLAN 4.19)."""
    meta = _store().last_message_meta(THREAD_SESSION_ID)
    return {"session_id": THREAD_SESSION_ID, "last_message_id": int(meta["id"]) if meta else 0}


@router.get("/live")
async def live() -> StreamingResponse:
    """Every thread turn as it happens, from whichever device sent it (app/thread_live.py): the
    chat stream's own events plus ``turn_start`` / ``turn_end``, each with ``_turn`` and ``_origin``."""
    hub = get_thread_live()
    queue = hub.subscribe()

    async def source():
        try:
            yield "event: ready\ndata: {}\n\n"
            while True:
                try:
                    frame = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if frame is None:  # dropped as stalled: end, the client reconnects
                    return
                event, data = frame
                yield f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"
        finally:
            hub.unsubscribe(queue)

    return StreamingResponse(source(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/messages")
def list_messages(before_id: Optional[int] = Query(default=None, ge=1),
                  after_id: Optional[int] = Query(default=None, ge=0),
                  limit: int = Query(default=80, ge=1, le=500)) -> dict[str, Any]:
    """The newest ``limit`` messages older than ``before_id``, oldest first, with ``id`` and
    ``created_at``; ``has_more`` says whether older ones remain. With ``after_id`` instead: the
    messages newer than it, oldest first (``has_more``: newer ones remain past ``limit``). A row
    that opens an episode carries ``episode_start`` (the episode's id): the episode's first
    message is the first one after its ``start_after_message_id``, which only the server can tell
    on a page's first row."""
    store = _store()
    if after_id is not None:
        messages, more = store.get_messages_after(THREAD_SESSION_ID, after_id, limit=limit)
    else:
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


@router.delete("/episodes/{episode_id}")
def delete_episode(episode_id: int) -> dict[str, Any]:
    """The sidebar's "Delete topic": the episode's messages, record and search entries go for good,
    and later digests that listed it are rebuilt without it (PLAN 4.18 phase 5)."""
    store = _store()
    if not any(e["id"] == episode_id for e in store.list_episodes(THREAD_SESSION_ID)):
        raise HTTPException(status_code=404, detail=f"No topic {episode_id} in the conversation.")
    return {"deleted": True, "episode_id": episode_id, **forget_episodes(store, [episode_id])}
