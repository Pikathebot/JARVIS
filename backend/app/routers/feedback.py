"""The "got it wrong" tap on a reply (PLAN §4 step 1): into the training log for the fine-tune."""
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.agent import training_log

router = APIRouter(prefix="/api/feedback", tags=["feedback"])


class WrongRequest(BaseModel):
    meant: str = Field(default="", max_length=2000)  # optional: what the user meant


@router.post("/{turn_id}/wrong", response_model=dict)
async def flag_wrong(turn_id: str, body: WrongRequest) -> dict[str, Any]:
    """Flag a reply; send again with ``meant`` to add what was meant (the later record wins)."""
    if not training_log.flag_wrong(turn_id, body.meant):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="That reply can't be flagged any more (too old, or from before a restart).")
    return {"saved": True}


@router.delete("/{turn_id}/wrong", response_model=dict)
async def unflag(turn_id: str) -> dict[str, Any]:
    if not training_log.unflag(turn_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No turn {turn_id}.")
    return {"saved": True}
