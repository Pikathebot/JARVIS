"""Google sign-in endpoints (PLAN 4.16 phase B). The client polls /status after /sign-in."""
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, status

from app.google import auth

logger = logging.getLogger("jarvis.routers.google")

router = APIRouter(prefix="/api/google", tags=["google"])


def _synced_at() -> dict[str, Any]:
    """When each sync last finished (epoch seconds), for Settings -> Accounts; None = never."""
    from app.agent.tools import calendar_events
    from app.google import tasks_sync

    return {"tasks_synced_at": tasks_sync._load_state().get("last_sync"),
            "calendar_synced_at": calendar_events.load().get("last_sync")}


@router.get("/status")
def google_status() -> dict[str, Any]:
    return {**auth.status(), **_synced_at()}


@router.post("/sign-in")
def google_sign_in() -> dict[str, Any]:
    """Opens the browser at Google's sign-in page; the token arrives on the loopback redirect."""
    try:
        url = auth.start_sign_in()
    except auth.GoogleNotConnected as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return {"started": True, "url": url}


@router.post("/sign-out")
def google_sign_out() -> dict[str, Any]:
    return {"signed_out": auth.sign_out()}


@router.post("/sync")
def google_sync_now() -> dict[str, Any]:
    """Both syncs now (Settings' "Sync now"); each side's counts, or why it didn't run."""
    from app.google import calendar_sync, tasks_sync

    if not auth.status()["signed_in"]:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Not signed in to Google.")
    out: dict[str, Any] = {}
    for name, mod in (("tasks", tasks_sync), ("calendar", calendar_sync)):
        try:
            out[name] = mod.sync()
        except Exception as exc:  # GoogleNotConnected, SyncRefused, offline: reported, not raised
            logger.warning("Sync now (%s) failed: %s", name, exc)
            out[name] = {"error": str(exc)}
    return {**out, **_synced_at()}
