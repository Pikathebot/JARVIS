"""
``POST /api/rag/trace``: run a workspace's retrieval for one question and return every stage,
without asking the model (PLAN §4.8a step 1). With ``expect`` (the right answer, e.g. a name)
it also says which indexed passages contain it and the stage where they were lost.
``scripts/rag_name_check.py`` drives it over a test set. Read-only.
"""
import asyncio
from typing import Any, Optional

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/api/rag", tags=["rag"])

_retriever = None


def _get_retriever():
    global _retriever
    if _retriever is None:
        from app.rag.retriever import HybridRetriever
        _retriever = HybridRetriever()
    return _retriever


class TraceRequest(BaseModel):
    project_id: str
    query: str
    expect: Optional[str] = None
    top_k: Optional[int] = None


@router.post("/trace", response_model=dict[str, Any])
async def trace_retrieval(req: TraceRequest) -> dict[str, Any]:
    from app.config import settings
    from app.rag import trace as rag_trace

    def run() -> dict[str, Any]:
        retriever = _get_retriever()
        record: dict[str, Any] = {}
        final = retriever.retrieve(
            project_id=req.project_id, query=req.query,
            top_k=req.top_k or settings.context_tier3_max_chunks, trace=record,
        )
        record["sent_to_model"] = [{"id": rag_trace.chunk_id(c), "text": rag_trace.chunk_text(c)} for c in final]
        if req.expect:
            holding = retriever.keyword_store.chunks_containing(req.project_id, req.expect)
            record["expect"] = {"text": req.expect, **rag_trace.locate(record, holding)}
        return record

    return await asyncio.to_thread(run)
