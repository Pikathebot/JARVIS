import asyncio
import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.awareness.briefing import build_briefing, format_spoken
from app.awareness.monitor import AwarenessMonitor
from app.awareness.observations import Observation, Severity
from app.persona import persona_manager

logger = logging.getLogger("jarvis.routers.awareness")

router = APIRouter(prefix="/api/awareness", tags=["awareness"])


def get_monitor() -> AwarenessMonitor:
    try:
        from app.main import awareness_monitor

        return awareness_monitor
    except Exception:
        return AwarenessMonitor()


class AwarenessConfigRequest(BaseModel):
    enabled: Optional[bool] = None
    poll_seconds: Optional[float] = Field(default=None, ge=2.0, le=3600.0)
    restate_cooldown_seconds: Optional[float] = Field(default=None, ge=0.0, le=86400.0)
    min_speak_severity: Optional[str] = Field(
        default=None, description="info | notice | warning | critical"
    )
    actions_enabled: Optional[bool] = Field(
        default=None, description="Whether critical observations may trigger a real action"
    )


def _render(observation, persona) -> dict[str, Any]:
    payload = observation.to_dict()
    payload["spoken"] = format_spoken(observation, persona)
    payload["speak"] = get_monitor().should_speak(observation)
    return payload


@router.get("/status", response_model=dict)
async def awareness_status() -> dict[str, Any]:
    """Current system snapshot plus what the monitor is actively watching."""
    monitor = get_monitor()
    snapshot = monitor.last_snapshot or monitor.collect_snapshot()
    return {
        "snapshot": snapshot.to_dict(),
        "monitor": monitor.status(),
    }


@router.post("/poll", response_model=dict)
async def poll_now() -> dict[str, Any]:
    """Force an immediate evaluation instead of waiting for the next tick."""
    monitor = get_monitor()
    persona = persona_manager.get_active()
    observations = await monitor.poll_once()
    return {
        "observations": [_render(o, persona) for o in observations],
        "snapshot": (monitor.last_snapshot.to_dict() if monitor.last_snapshot else {}),
    }


@router.post("/test", response_model=dict)
async def emit_test_observation() -> dict[str, Any]:
    """Put a harmless notice on the stream (Settings -> Developer), to see the tray and the remote push."""
    observation = get_monitor().emit(Observation(
        kind="test",
        severity=Severity.NOTICE,
        title="Test notification",
        detail="Sent from Settings > Developer. Dismiss it with the X.",
    ))
    return {"id": observation.id}


@router.get("/observations", response_model=dict)
async def list_observations(
    limit: int = Query(default=20, ge=1, le=100),
    since_seq: int = Query(default=0, ge=0, description="Last observation seq already seen"),
) -> dict[str, Any]:
    """Recent observations, newest last. Page with the seq of the last one seen."""
    monitor = get_monitor()
    persona = persona_manager.get_active()
    items = monitor.recent(limit=limit, since_seq=since_seq)
    return {
        "observations": [_render(o, persona) for o in items],
        "active_conditions": monitor.active_observations(),
        "latest_seq": monitor.status()["latest_seq"],
    }


@router.post("/observations/{observation_id}/ack", response_model=dict)
async def acknowledge_observation(observation_id: str) -> dict[str, Any]:
    """Mark one observation as seen so the UI stops surfacing it."""
    if not get_monitor().acknowledge(observation_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No observation with id '{observation_id}'.",
        )
    return {"acknowledged": True, "id": observation_id}


@router.get("/stream")
async def stream_observations() -> StreamingResponse:
    """
    Server-sent stream of observations as they are noticed.

    Keepalive comments are emitted so an idle connection is not dropped by
    intermediaries during quiet periods.
    """
    monitor = get_monitor()
    queue = monitor.subscribe()

    async def event_source():
        try:
            yield f"event: ready\ndata: {json.dumps(monitor.status())}\n\n"
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=20.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue

                persona = persona_manager.get_active()
                payload = dict(payload)
                payload["spoken"] = (payload.get("spoken") or "").replace(
                    "{address}",
                    (
                        f", {persona.address_term}"
                        if getattr(persona, "address_term", "")
                        else ""
                    ),
                )
                yield f"event: observation\ndata: {json.dumps(payload)}\n\n"
        except asyncio.CancelledError:
            raise
        finally:
            monitor.unsubscribe(queue)

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/briefing", response_model=dict)
async def briefing() -> dict[str, Any]:
    """
    A spoken-ready status briefing. Assembled from telemetry rather than
    generated, so it is instant and its numbers are always real.
    """
    monitor = get_monitor()
    persona = persona_manager.get_active()
    snapshot = monitor.last_snapshot or monitor.collect_snapshot()

    payload = build_briefing(
        snapshot=snapshot,
        persona=persona,
        active_conditions=monitor.active_observations(),
    )
    payload["persona_id"] = persona_manager.active_id
    payload["snapshot"] = snapshot.to_dict()
    return payload


@router.get("/config", response_model=dict)
async def get_config() -> dict[str, Any]:
    return get_monitor().status()


@router.patch("/config", response_model=dict)
async def update_config(req: AwarenessConfigRequest) -> dict[str, Any]:
    """Tune how often Jarvis looks, and how loud a problem must be to speak up."""
    monitor = get_monitor()

    if req.enabled is not None:
        monitor.enabled = req.enabled
    if req.poll_seconds is not None:
        monitor.poll_seconds = req.poll_seconds
    if req.restate_cooldown_seconds is not None:
        monitor.restate_cooldown_seconds = req.restate_cooldown_seconds
    if req.min_speak_severity is not None:
        try:
            monitor.min_speak_severity = Severity(req.min_speak_severity.lower())
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="min_speak_severity must be one of: "
                + ", ".join(s.value for s in Severity),
            )
    if req.actions_enabled is not None:
        monitor.actions_enabled = req.actions_enabled

    return monitor.status()
