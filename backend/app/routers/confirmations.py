"""
Pending confirmations: what Jarvis is waiting on an answer for, and the way to say no.

Approving is already a chat turn (``approved_action_ids``); denying used to be client-only,
so the backend kept waiting and would eventually announce a timeout for something the user
had already declined. ``POST .../deny`` closes that gap.
"""
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, status

from app.agent.confirmations import get_confirmation_registry

logger = logging.getLogger("jarvis.routers.confirmations")

router = APIRouter(prefix="/api/confirmations", tags=["confirmations"])


@router.get("", response_model=dict)
async def list_pending(session_id: str | None = None) -> dict[str, Any]:
    """Everything asked and not yet answered, with how long each has left."""
    registry = get_confirmation_registry()
    return {
        "timeout_seconds": registry.timeout_seconds,
        "pending": [p.to_dict() for p in registry.pending(session_id)],
    }


@router.post("/{action_id}/deny", response_model=dict)
async def deny(action_id: str) -> dict[str, Any]:
    """The user declined: drop the ask so no timeout is announced for it."""
    entry = get_confirmation_registry().deny(action_id)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No pending confirmation with id {action_id}.",
        )
    return {"denied": entry.to_dict()}
