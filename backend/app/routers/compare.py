"""In-app compare (PLAN 4.13 #8): poll a second take on a reply, then vote on it."""
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.agent import compare

router = APIRouter(prefix="/api/compare", tags=["compare"])


class VoteRequest(BaseModel):
    choice: str  # original / alternative / tie / dismiss


@router.get("/{compare_id}", response_model=dict)
async def get_compare(compare_id: str) -> dict[str, Any]:
    """judging / writing (poll again), ready (with the alternative), none / failed (stop polling)."""
    c = compare.get(compare_id)
    if c is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No compare {compare_id}.")
    return c.public()


@router.post("/{compare_id}/vote", response_model=dict)
async def vote(compare_id: str, body: VoteRequest) -> dict[str, Any]:
    result = compare.vote(compare_id, body.choice)
    if result is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="Nothing to vote on (unknown, not ready, or already voted).")
    return result
