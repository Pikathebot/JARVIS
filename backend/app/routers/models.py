"""
Model catalogue endpoints: what GGUFs exist, which one each slot uses, and how to change that.

Until now the two slots were pinned by ``LLAMA_MAIN_MODEL_PATH`` / ``LLAMA_FAST_MODEL_PATH`` and
could only be changed by editing ``.env`` and restarting. These endpoints make the choice a
runtime one, persisted through ``ModelCatalog``.
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.agent.model_catalog import SLOTS, get_model_catalog, to_dict

logger = logging.getLogger("jarvis.routers.models")

router = APIRouter(prefix="/api/models", tags=["models"])


class ModelSelectRequest(BaseModel):
    slot: str = Field(default="main", description="Which slot to point at this model: 'main' or 'fast'")
    model_id: str = Field(description="Catalogue id, i.e. the repo-relative path such as models/Foo.gguf")
    activate: bool = Field(
        default=True,
        description=(
            "Restart llama-server onto the new model straight away. False records the choice "
            "for the next time that slot is loaded, without interrupting a running generation."
        ),
    )


def _runtime_manager():
    from app.agent.runtime_process_manager import get_runtime_process_manager

    return get_runtime_process_manager()


@router.get("")
@router.get("/")
async def list_models():
    """
    Every selectable model, recommended ones first.

    ``recommended`` marks the files sitting directly in ``models/`` -- the deliberately installed
    ones -- as opposed to those nested inside vendor download trees like ``models/unsloth/``.
    Clients should present the list in the order given and default to the first recommended entry.
    Projectors (``mmproj-*.gguf``) are reported separately because they are vision adapters loaded
    alongside a model, not models you can chat with.
    """
    catalog = get_model_catalog()
    manager = _runtime_manager()

    return {
        "models": [to_dict(info) for info in catalog.discover()],
        "projectors": [to_dict(info) for info in catalog.projectors()],
        "selection": catalog.selection(),
        "slots": list(SLOTS),
        "loaded_slot": manager.current_model_kind,
        "loaded_projector": str(manager.loaded_projector.name) if manager.loaded_projector else None,
        # The file actually serving the slot and how far it is from the slot's configuration:
        # the launch ladder may have dropped the projector, halved the context, or (main only)
        # swapped in the fast model when the card was too full. None means "as configured".
        "served_model": manager.served_model.name if manager.served_model else None,
        "served_ctx_size": manager.served_ctx_size,
        "launch_adjustment": manager.launch_adjustment,
        "externally_managed": manager.is_externally_managed,
        "captioner": _captioner_status(),
        "embedder": _embedder_status(),
    }


def _embedder_status() -> dict:
    """The CPU embedding sidecar behind workspace RAG (app/rag/embedding_sidecar.py)."""
    try:
        from app.rag.embedding_sidecar import get_embedding_sidecar

        return get_embedding_sidecar().status()
    except Exception as e:  # pragma: no cover - defensive
        return {"enabled": False, "available": False, "running": False, "error": str(e)}


def _captioner_status() -> dict:
    """The CPU image-captioner sidecar that stands in for a projector when the chat model has none."""
    try:
        from app.agent.captioner import get_image_captioner

        return get_image_captioner().status()
    except Exception as e:  # pragma: no cover - defensive
        return {"enabled": False, "available": False, "running": False, "error": str(e)}


@router.post("/select")
async def select_model(req: ModelSelectRequest):
    """Points a slot at a model, and by default restarts llama-server onto it."""
    catalog = get_model_catalog()

    try:
        info = catalog.select(req.slot, req.model_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))

    activated = False
    error: Optional[str] = None
    if req.activate:
        try:
            activated = await _runtime_manager().reload(req.slot)
        except Exception as exc:
            # The selection is already persisted, so report the load failure without discarding
            # the choice -- the user can retry, or pick something that fits in VRAM.
            logger.error("Failed to load model '%s' into slot '%s': %s", req.model_id, req.slot, exc)
            error = str(exc)

    return {
        "selected": to_dict(info),
        "selection": catalog.selection(),
        "activated": activated,
        "error": error,
    }


@router.delete("/select/{slot}")
async def clear_model_selection(slot: str):
    """Drops a slot's override so it falls back to the path configured in .env."""
    if slot not in SLOTS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown slot '{slot}'")

    catalog = get_model_catalog()
    catalog.clear(slot)
    return {"selection": catalog.selection()}
