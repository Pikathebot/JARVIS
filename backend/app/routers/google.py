"""Google sign-in endpoints (PLAN 4.16 phase B). The client polls /status after /sign-in."""
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, status

from app.google import auth

logger = logging.getLogger("jarvis.routers.google")

router = APIRouter(prefix="/api/google", tags=["google"])


@router.get("/status")
def google_status() -> dict[str, Any]:
    return auth.status()


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
