"""RAG trace (PLAN §4.8a step 1, 2026-10-01): every retrieval stage recorded, the stage where an
expected name is lost, and names in an answer that no passage given to the model contains."""
import json

from app.rag import trace as rag_trace
from app.rag.embeddings import EmbeddingService, RerankerService
from app.rag.indexer import ProjectIndexer
from app.rag.keyword_store import KeywordSearchService
from app.rag.retriever import HybridRetriever
from app.rag.vector_store import VectorStoreService
from app.database.models import Project
from tests.test_rag_retriever import rag_hybrid_env  # noqa: F401  (fixture)


def test_names_the_passages_do_not_contain_are_flagged():
    chunks = [{"content": "Arun's younger sister is Meera. She lives in Madurai."}]
    answer = "According to the document, Arun's younger sister is Kavya. She lives in Madurai."
    assert rag_trace.names_not_in(answer, chunks) == ["Kavya"]
    assert rag_trace.names_not_in("His sister is Meera.", chunks) == []


def _trace(**stages):
    return {"top_k": 5, **{k: [{"id": i} for i in v] for k, v in stages.items()}}


def test_locate_names_the_first_stage_that_lost_it():
    holding = [{"chunk_id": "c7", "content": "Meera"}]
    assert rag_trace.locate(_trace(), [])["verdict"].startswith("not indexed")
    assert rag_trace.locate(_trace(semantic=["c1"], keyword=["c2"]), holding)["verdict"].startswith("neither")
    cut = rag_trace.locate({**_trace(semantic=["c7"], reranked=["c1", "c7"], final=["c1"]), "floor": 0.62}, holding)
    assert cut["verdict"].startswith("cut by the reranker") and cut["stages"]["semantic_top"]
    flat = rag_trace.locate({**_trace(keyword=["c7"], reranked=["c7"]), "stopped": "flat relevance: ..."}, holding)
    assert flat["verdict"] == "cut by the reranker: flat relevance: ..."
    ok = rag_trace.locate(_trace(keyword=["c7"], reranked=["c7"], final=["c7"]), holding)
    assert ok["verdict"].startswith("retrieved")


def test_retrieval_fills_every_stage_and_the_name_is_located(rag_hybrid_env, tmp_path):  # noqa: F811
    session_factory, root = rag_hybrid_env
    with session_factory() as s:
        s.add(Project(id="p_names", name="Names", workspace_path=str(root)))
        s.commit()
    files = root / "projects" / "p_names" / "files"
    files.mkdir(parents=True)
    (files / "family.md").write_text(
        "# Family\n\nArun has a younger sister called Meera, who studies medicine in Madurai.\n",
        encoding="utf-8")
    (files / "work.md").write_text("# Work\n\nThe quarterly report is due in March.\n", encoding="utf-8")

    emb = EmbeddingService(device="cpu", embedding_dim=64)
    vectors = VectorStoreService(workspace_root=root, embedding_service=emb)
    keywords = KeywordSearchService(session_factory=session_factory)
    ProjectIndexer(session_factory=session_factory, embedding_service=emb, vector_store=vectors,
                   keyword_store=keywords).index_project("p_names")
    retriever = HybridRetriever(embedding_service=emb, vector_store=vectors, keyword_store=keywords,
                                reranker=RerankerService(device="cpu"))

    record: dict = {}
    final = retriever.retrieve("p_names", "what is the name of Arun's younger sister?", top_k=3, trace=record)
    assert record["query"] == "what is the name of Arun's younger sister?"
    assert record["keyword"] and "reranked" in record and "final" in record
    where = rag_trace.locate(record, keywords.chunks_containing("p_names", "meera"))
    assert where["stages"]["indexed"] and where["stages"]["keyword_top"]
    assert where["verdict"].startswith("retrieved") == bool(final)

    gated: dict = {}
    assert retriever.retrieve("p_names", "hi", trace=gated) == []
    assert gated["stopped"].startswith("should_retrieve")


def test_trace_lines_are_appended_as_json(tmp_path, monkeypatch):
    monkeypatch.setattr(rag_trace, "TRACE_PATH", tmp_path / "t.jsonl")
    record = rag_trace.finish({"query": "q"}, [{"chunk_id": "a", "content": "Meera"}], [],
                              answer="It is Kavya.", tools_used=[{"tool": "read_file"}])
    rag_trace.write(record)
    [line] = (tmp_path / "t.jsonl").read_text(encoding="utf-8").splitlines()
    data = json.loads(line)
    assert data["sent_to_model"] == [{"id": "a", "text": "Meera"}]
    assert data["names_not_in_passages"] == ["Kavya"] and data["tools_used"] == ["read_file"]


class _FakeRetriever:
    def retrieve(self, project_id, query, top_k=5, trace=None, **_):
        chunk = {"chunk_id": "c1", "file_path": "family.md", "content": "Arun's sister is Meera."}
        if trace is not None:
            trace.update(query=query, project_id=project_id, final=[rag_trace.brief(chunk)])
        return [chunk]


async def _turn(orch, project_id, session_id):
    async def fake_loop(*args, **kwargs):
        yield {"event": "done", "data": {"response": "His sister is Kavya.", "tools_used": []}}
    orch._run_provider_stream_loop = fake_loop
    return [ev async for ev in orch.run_stream("who is Arun's sister?", session_id=session_id,
                                              project_id=project_id, chat_mode="WORKSPACE")]


import pytest  # noqa: E402


@pytest.mark.asyncio
async def test_a_workspace_turn_is_traced_and_an_off_the_record_one_is_not(tmp_path, monkeypatch):
    import uuid
    from app.agent.orchestrator import AgentOrchestrator
    from app.database import SessionLocal
    from app.memory.ephemeral import EphemeralMemoryStore, EphemeralRegistry
    from app.memory.store import MemoryStore

    from sqlmodel import SQLModel
    from app.database.session import engine
    SQLModel.metadata.create_all(engine)  # the session-wide test DB gets tables from whoever needs them
    monkeypatch.setattr(rag_trace, "TRACE_PATH", tmp_path / "t.jsonl")
    pid = f"p-trace-{uuid.uuid4().hex[:6]}"
    (tmp_path / "ws").mkdir()
    with SessionLocal() as s:
        s.add(Project(id=pid, name="Trace", workspace_path=str(tmp_path / "ws")))
        s.commit()

    store = MemoryStore(session_factory=SessionLocal)
    await _turn(AgentOrchestrator(memory_store=store, retriever=_FakeRetriever()), pid, f"s-{pid}")
    [line] = (tmp_path / "t.jsonl").read_text(encoding="utf-8").splitlines()
    data = json.loads(line)
    assert data["query"] == "who is Arun's sister?" and data["answer"] == "His sister is Kavya."
    assert data["sent_to_model"][0]["text"] == "Arun's sister is Meera."
    assert data["names_not_in_passages"] == ["Kavya"]

    eph = EphemeralMemoryStore(store, registry=EphemeralRegistry())
    await _turn(AgentOrchestrator(memory_store=eph, retriever=_FakeRetriever()), pid, f"eph-{pid}")
    assert len((tmp_path / "t.jsonl").read_text(encoding="utf-8").splitlines()) == 1


@pytest.mark.asyncio
async def test_trace_endpoint_reports_where_the_name_is(monkeypatch):
    import httpx
    from app.main import app
    from app.routers import rag_debug

    class Keywords:
        def chunks_containing(self, project_id, needle):
            return [{"chunk_id": "c1", "content": "Arun's sister is Meera."}] if needle == "Meera" else []

    retriever = _FakeRetriever()
    retriever.keyword_store = Keywords()
    monkeypatch.setattr(rag_debug, "_retriever", retriever)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        res = await client.post("/api/rag/trace", json={"project_id": "p", "query": "who is Arun's sister?", "expect": "Meera"})
        missing = await client.post("/api/rag/trace", json={"project_id": "p", "query": "x?", "expect": "Kavya"})
    assert res.status_code == 200
    body = res.json()
    assert body["expect"]["verdict"].startswith("retrieved") and body["sent_to_model"][0]["id"] == "c1"
    assert missing.json()["expect"]["verdict"].startswith("not indexed")


class _EmptyRetriever:
    def retrieve(self, project_id, query, top_k=5, trace=None, **_):
        return []


@pytest.mark.asyncio
async def test_passages_carry_the_answer_rule_and_a_miss_says_so(tmp_path, monkeypatch):
    import uuid
    from sqlmodel import SQLModel
    from app.agent.orchestrator import AgentOrchestrator
    from app.database import SessionLocal
    from app.database.session import engine
    from app.memory.context_manager import NO_PASSAGES_NOTE, WORKSPACE_PASSAGES_RULE
    from app.memory.store import MemoryStore

    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(rag_trace, "TRACE_PATH", tmp_path / "t.jsonl")
    pid = f"p-rule-{uuid.uuid4().hex[:6]}"
    (tmp_path / "ws").mkdir()
    with SessionLocal() as s:
        s.add(Project(id=pid, name="Rule", workspace_path=str(tmp_path / "ws")))
        s.commit()

    async def last_user_text(retriever):
        sent = {}
        orch = AgentOrchestrator(memory_store=MemoryStore(session_factory=SessionLocal), retriever=retriever)

        async def fake_loop(*args, **kwargs):
            sent.update(kwargs)
            yield {"event": "done", "data": {"response": "ok", "tools_used": []}}
        orch._run_provider_stream_loop = fake_loop
        async for _ in orch.run_stream("what is the World Tree a reference to?", session_id=f"s-{uuid.uuid4().hex[:6]}",
                                       project_id=pid, chat_mode="WORKSPACE"):
            pass
        content = sent["conversation_messages"][-1]["content"]
        return content if isinstance(content, str) else " ".join(p.get("text", "") for p in content)

    found = await last_user_text(_FakeRetriever())
    assert WORKSPACE_PASSAGES_RULE in found and NO_PASSAGES_NOTE not in found
    missed = await last_user_text(_EmptyRetriever())
    assert NO_PASSAGES_NOTE in missed and WORKSPACE_PASSAGES_RULE not in missed
