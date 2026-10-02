"""
Model catalogue endpoints: what GGUFs exist, which one each slot uses, and how to change that.

Until now the two slots were pinned by ``LLAMA_MAIN_MODEL_PATH`` / ``LLAMA_FAST_MODEL_PATH`` and
could only be changed by editing ``.env`` and restarting. These endpoints make the choice a
runtime one, persisted through ``ModelCatalog``.
"""

import asyncio
import logging
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.agent import model_hub
from app.agent.model_catalog import REPO_ROOT, SLOTS, ModelCatalog, ModelInfo, get_model_catalog, to_dict

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


class DownloadRequest(BaseModel):
    repo: str = Field(description="HuggingFace repo id, owner/name")
    file: str = Field(description="The model file as GET hub/files names it (a split set by its first part)")
    projector: Optional[str] = Field(
        default=None, description="A projector from the same repo to download beside it, for vision"
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

    budget = model_hub.current_budget_mb()
    models = []
    for info in catalog.discover():
        entry = to_dict(info)
        entry["fit"] = _local_fit(catalog, info, budget)
        models.append(entry)

    return {
        "models": models,
        "fit_budget_mb": budget,
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


# --- fit labels ------------------------------------------------------------------------------

def _local_fit(catalog: ModelCatalog, info: ModelInfo, budget: Optional[float]) -> dict[str, Any]:
    """Fit per slot for a model on disk: what it measured here when it has run at that slot's
    context, else the estimate from its size and its GGUF header."""
    path = REPO_ROOT / info.id
    projector = REPO_ROOT / info.projector if info.projector else None
    try:
        proj_bytes = projector.stat().st_size if projector else None
        model_bytes = path.stat().st_size
    except OSError:
        return {}
    return model_hub.slot_fits(
        model_bytes, proj_bytes, model_hub.local_kv_rate(path), budget,
        learned=lambda _slot, ctx: catalog.launch_cost(path, projector, ctx),
    )


def _hub_fit(name: str, size: int, projector: Optional[model_hub.HubFile], kv_rate, budget) -> dict[str, Any]:
    """Fit per slot for a file not downloaded yet, text-only and (if the repo has a projector)
    with it. A launch measured here wins when the same file has run before."""
    catalog = get_model_catalog()

    def learned(proj_name):
        def lookup(_slot, ctx):
            return catalog.launch_cost(Path(name), Path(proj_name) if proj_name else None, ctx)
        return lookup

    out = {"text": model_hub.slot_fits(size, None, kv_rate, budget, learned(None))}
    if projector is not None:
        # Measured launches are keyed by projector file name, so a measured "with vision"
        # figure only exists for a projector of the same name (ours is a Q8_0 copy).
        out["vision"] = model_hub.slot_fits(size, projector.size_bytes, kv_rate, budget, learned(projector.name))
    return out


def _hub_error(exc: Exception) -> HTTPException:
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403, 404):
            return HTTPException(status.HTTP_404_NOT_FOUND, "Repo not found on HuggingFace, or gated (needs a login)")
        return HTTPException(status.HTTP_502_BAD_GATEWAY, f"HuggingFace answered {code}")
    return HTTPException(status.HTTP_502_BAD_GATEWAY, f"Could not reach HuggingFace: {exc}")


def _require_repo(repo: str) -> None:
    if not model_hub.valid_repo(repo):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Not a HuggingFace repo id: {repo!r}")


def _on_disk(repo: str, name: str, size: int) -> bool:
    """Already here: in the folder a download would use, or anywhere under models/ with the
    same name and size (the hand-made folders like models/qwen3.5-9b/ predate downloads)."""
    manager = model_hub.get_download_manager()
    if (manager.dest_dir_for(repo) / name).exists():
        return True
    return any(p.stat().st_size == size for p in manager.models_dir.rglob(name) if p.is_file())


# --- HuggingFace: curated list, search, a repo's files ----------------------------------------

@router.get("/hub/curated")
async def hub_curated():
    """The models we know work here, with sizes and fit labels. Their KV rates are in the
    curated file, so this costs one tree request per repo and no header reads."""
    hub = model_hub.get_hub_client()
    budget = model_hub.current_budget_mb()
    entries = model_hub.load_curated()

    async def describe(entry: dict) -> Optional[dict]:
        try:
            files = await hub.files(entry["repo"])
        except Exception as exc:
            logger.info("Curated repo %s unavailable: %s", entry["repo"], exc)
            return None
        model = next((f for f in files if f.name == entry["file"]), None)
        if model is None:
            return None
        projector = next((f for f in files if f.projector and f.name == entry.get("projector")), None)
        return {
            "repo": entry["repo"],
            "slot": entry.get("slot"),
            "note": entry.get("note"),
            "file": model.to_dict(),
            "projector": projector.to_dict() if projector else None,
            "downloaded": _on_disk(entry["repo"], model.name, model.size_bytes),
            "fit": _hub_fit(model.name, model.size_bytes, projector, entry.get("kv_mib_per_1k"), budget),
        }

    described = await asyncio.gather(*(describe(e) for e in entries))
    return {"models": [d for d in described if d], "fit_budget_mb": budget}


@router.get("/hub/search")
async def hub_search(q: str = Query(min_length=2, max_length=100), limit: int = Query(default=20, ge=1, le=50)):
    """GGUF repos on HuggingFace matching ``q``, most downloaded first."""
    try:
        return {"results": await model_hub.get_hub_client().search(q, limit)}
    except Exception as exc:
        raise _hub_error(exc)


@router.get("/hub/files")
async def hub_files(repo: str):
    """
    A repo's GGUF files with a fit label per slot. The first call for a repo reads one file's
    header from HuggingFace (~5 s) to learn the model's KV size; later calls are cached.
    """
    _require_repo(repo)
    hub = model_hub.get_hub_client()
    try:
        files = await hub.files(repo)
    except Exception as exc:
        raise _hub_error(exc)
    kv = await hub.repo_kv_rate(repo, files)
    budget = model_hub.current_budget_mb()
    projectors = [f for f in files if f.projector]
    smallest_projector = min(projectors, key=lambda f: f.size_bytes) if projectors else None
    return {
        "repo": repo,
        "kv_mib_per_1k": kv,
        "fit_budget_mb": budget,
        "models": [
            {**f.to_dict(), "downloaded": _on_disk(repo, f.name, f.size_bytes),
             "fit": _hub_fit(f.name, f.size_bytes, smallest_projector, kv, budget)}
            for f in files if not f.projector
        ],
        "projectors": [{**f.to_dict(), "downloaded": _on_disk(repo, f.name, f.size_bytes)} for f in projectors],
    }


# --- downloads ---------------------------------------------------------------------------------

@router.post("/download")
async def start_download(req: DownloadRequest):
    """Starts (or queues) a download into models/<repo name>/. Poll GET downloads for progress."""
    _require_repo(req.repo)
    try:
        files = await model_hub.get_hub_client().files(req.repo)
    except Exception as exc:
        raise _hub_error(exc)
    model = next((f for f in files if f.name == req.file and not f.projector), None)
    if model is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{req.file} is not a model in {req.repo}")
    parts = list(model.parts)
    if req.projector:
        projector = next((f for f in files if f.name == req.projector and f.projector), None)
        if projector is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"{req.projector} is not a projector in {req.repo}")
        parts += projector.parts
    try:
        job = model_hub.get_download_manager().start(req.repo, parts)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return {"download": job.to_dict()}


@router.get("/downloads")
async def list_downloads():
    return {"downloads": [j.to_dict() for j in model_hub.get_download_manager().jobs()]}


@router.delete("/downloads/{job_id}")
async def cancel_download(job_id: str):
    """Stops a download. Its .part file stays, so starting it again resumes."""
    manager = model_hub.get_download_manager()
    if manager.get(job_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such download")
    return {"cancelled": manager.cancel(job_id), "download": manager.get(job_id).to_dict()}
