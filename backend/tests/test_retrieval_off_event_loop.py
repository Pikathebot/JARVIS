"""Retrieval embeds over HTTP, may cold-start the embedding sidecar and reranks; run on the event
loop it froze streaming and every other request for its whole duration (measured: 56-136 ms warm,
~1 s cold). It must run in a worker thread."""
import asyncio
import threading
import time
import uuid

import pytest
from sqlmodel import SQLModel

from app.database import SessionLocal
from app.database.models import Project
from app.database.session import engine

BLOCK_S = 0.4


class _SlowRetriever:
    def __init__(self):
        self.thread_id = None

    def retrieve(self, project_id, query, top_k=5, trace=None, **_):
        self.thread_id = threading.get_ident()
        time.sleep(BLOCK_S)  # stands in for a blocking embed / sidecar start / rerank
        return []


@pytest.mark.asyncio
async def test_workspace_retrieval_does_not_block_the_event_loop(tmp_path):
    from app.agent.orchestrator import AgentOrchestrator
    from app.memory.store import MemoryStore

    SQLModel.metadata.create_all(engine)
    pid = f"p-loop-{uuid.uuid4().hex[:6]}"
    (tmp_path / "ws").mkdir()
    with SessionLocal() as s:
        s.add(Project(id=pid, name="Loop", workspace_path=str(tmp_path / "ws")))
        s.commit()

    retriever = _SlowRetriever()
    orch = AgentOrchestrator(memory_store=MemoryStore(session_factory=SessionLocal), retriever=retriever)

    async def fake_loop(*args, **kwargs):
        yield {"event": "done", "data": {"response": "ok", "tools_used": []}}
    orch._run_provider_stream_loop = fake_loop

    gaps: list[float] = []
    stop = False

    async def heartbeat():
        last = time.perf_counter()
        while not stop:
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            gaps.append(now - last - 0.01)
            last = now

    beat = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.05)
    events = [ev async for ev in orch.run_stream(
        "where does the governor unload the model?", session_id=f"s-{pid}", project_id=pid, chat_mode="WORKSPACE")]
    stop = True
    await beat

    assert events, "the turn produced no events"
    assert retriever.thread_id is not None, "retrieval never ran (the turn was gated off)"
    assert retriever.thread_id != threading.get_ident(), "retrieve() ran on the event-loop thread"
    assert max(gaps) < BLOCK_S / 2, f"event loop stalled {max(gaps) * 1000:.0f} ms during retrieval"
