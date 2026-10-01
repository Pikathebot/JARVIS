import logging
from typing import Any, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.memory.manager import (
    MemoryManager,
    MemoryCategory,
    MemoryCreate,
    MemoryUpdate,
    MemoryRead,
    CandidateMemory,
)
from app.memory.store import MemoryStore
from app.rag.embeddings import EmbeddingService
from app.agent.model_router import ModelRouter

logger = logging.getLogger("jarvis.routers.memories")

router = APIRouter(prefix="/api/memories", tags=["memories"])


# Dependency injection providers conforming to Amendment 1
def get_memory_store() -> MemoryStore:
    try:
        from app.main import memory_store
        return memory_store
    except Exception:
        return MemoryStore()


def get_embedding_service() -> EmbeddingService:
    return EmbeddingService(device="cpu")


def get_model_router() -> ModelRouter:
    try:
        from app.main import model_router
        return model_router
    except Exception:
        return ModelRouter()


def get_memory_manager(
    memory_store: MemoryStore = Depends(get_memory_store),
    embedding_service: EmbeddingService = Depends(get_embedding_service),
    model_router: ModelRouter = Depends(get_model_router),
) -> MemoryManager:
    return MemoryManager(
        memory_store=memory_store,
        embedding_service=embedding_service,
        model_router=model_router,
    )


class DetectMemoriesRequest(BaseModel):
    messages: list[dict[str, Any]] = Field(..., description="List of chat message dicts")


class DeduplicateResponse(BaseModel):
    deduplicated_count: int


@router.get("", response_model=list[dict[str, Any]])
def list_or_search_memories(
    query: Optional[str] = Query(default=None, description="Search query for semantic retrieval"),
    category: Optional[MemoryCategory] = Query(default=None, description="Filter by category"),
    project_id: Optional[str] = Query(default=None, description="Filter by project ID"),
    limit: int = Query(default=20, ge=1, le=100),
    manager: MemoryManager = Depends(get_memory_manager),
):
    """
    Retrieve or search long-term memories with optional semantic search and category filtering.
    """
    return manager.retrieve_memories(
        query=query or "",
        limit=limit,
        project_id=project_id,
        category=category,
    )


@router.post("", response_model=MemoryRead, status_code=status.HTTP_201_CREATED)
def create_memory(
    payload: MemoryCreate,
    manager: MemoryManager = Depends(get_memory_manager),
):
    """
    Explicitly create and store a new long-term memory.
    """
    try:
        mem = manager.store_memory(payload)
        return MemoryRead(
            id=mem.id,
            project_id=mem.project_id,
            category=mem.category,
            content=mem.content,
            source_session_id=mem.source_session_id,
            confidence=mem.confidence,
            pinned=mem.pinned,
            created_at=mem.created_at,
            updated_at=mem.updated_at,
            last_used_at=mem.last_used_at,
        )
    except Exception as e:
        logger.error("Failed to create memory: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/bin", response_model=list[dict[str, Any]])
def list_forgotten_memories():
    """Memories the model forgot in the last 30 days, newest first, each restorable."""
    from app.memory import memory_bin
    return memory_bin.list_entries()


@router.post("/bin/{memory_id}/restore", response_model=dict[str, Any])
def restore_forgotten_memory(memory_id: str):
    """Put a forgotten memory back exactly as it was (the Undo on a "Forgot: ..." line)."""
    from app.agent.tools import memory as memory_tools
    from app.memory import memory_bin
    content = memory_bin.restore(memory_id, memory_tools.session_factory)
    if content is None:
        raise HTTPException(status_code=404, detail=f"No forgotten memory '{memory_id}' in the bin")
    return {"restored": memory_id, "content": content}


@router.get("/{memory_id}", response_model=MemoryRead)
def get_memory(
    memory_id: str,
    manager: MemoryManager = Depends(get_memory_manager),
):
    """
    Retrieve a specific memory by ID.
    """
    mem = manager.get_memory(memory_id)
    if not mem:
        raise HTTPException(status_code=404, detail=f"Memory '{memory_id}' not found")

    return MemoryRead(
        id=mem.id,
        project_id=mem.project_id,
        category=mem.category,
        content=mem.content,
        source_session_id=mem.source_session_id,
        confidence=mem.confidence,
        pinned=mem.pinned,
        created_at=mem.created_at,
        updated_at=mem.updated_at,
        last_used_at=mem.last_used_at,
    )


@router.patch("/{memory_id}", response_model=MemoryRead)
def update_memory_endpoint(
    memory_id: str,
    payload: MemoryUpdate,
    manager: MemoryManager = Depends(get_memory_manager),
):
    """
    Update fields of an existing memory.
    """
    try:
        updated = manager.update_memory(memory_id, payload)
        return MemoryRead(
            id=updated.id,
            project_id=updated.project_id,
            category=updated.category,
            content=updated.content,
            source_session_id=updated.source_session_id,
            confidence=updated.confidence,
            pinned=updated.pinned,
            created_at=updated.created_at,
            updated_at=updated.updated_at,
            last_used_at=updated.last_used_at,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Memory '{memory_id}' not found")
    except Exception as e:
        logger.error("Failed to update memory: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_memory_endpoint(
    memory_id: str,
    manager: MemoryManager = Depends(get_memory_manager),
):
    """
    Delete a memory record by ID.
    """
    deleted = manager.delete_memory(memory_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Memory '{memory_id}' not found")
    return None


@router.post("/deduplicate", response_model=DeduplicateResponse)
def deduplicate_memories_endpoint(
    project_id: Optional[str] = Query(default=None),
    similarity_threshold: float = Query(default=0.88, ge=0.5, le=1.0),
    manager: MemoryManager = Depends(get_memory_manager),
):
    """
    Execute semantic deduplication on stored memories.
    """
    count = manager.deduplicate_memories(
        project_id=project_id,
        similarity_threshold=similarity_threshold
    )
    return DeduplicateResponse(deduplicated_count=count)


@router.post("/detect", response_model=list[CandidateMemory])
def detect_candidate_memories_endpoint(
    payload: DetectMemoriesRequest,
    manager: MemoryManager = Depends(get_memory_manager),
):
    """
    Extract high-value candidate memories from message history.
    """
    return manager.detect_candidate_memories(payload.messages)
