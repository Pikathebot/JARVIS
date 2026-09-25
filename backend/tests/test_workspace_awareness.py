"""
What the model knows about a project workspace across turns: the per-turn file list, tool
results kept in history, and one shared Qdrant client per index (semantic RAG found 0 because
a second client could never open the folder).
"""
import pytest
from sqlmodel import SQLModel, Session, create_engine

from app.agent.orchestrator import TOOL_HISTORY_MAX_CHARS, AgentOrchestrator, workspace_file_listing
from app.memory.context_manager import ContextManager
from app.memory.ephemeral import EphemeralMemoryStore
from app.memory.store import MemoryStore


@pytest.fixture
def store(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'mem.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return MemoryStore(session_factory=lambda: Session(engine))


# --- 4b: the file list -------------------------------------------------------------------------

def test_listing_shows_user_files_and_skips_index_and_memory(tmp_path):
    (tmp_path / "prompt_principles.md").write_bytes(b"# Principles\n")
    (tmp_path / "files").mkdir()
    (tmp_path / "files" / "notes.txt").write_text("x", encoding="utf-8")
    (tmp_path / "indexes" / "qdrant").mkdir(parents=True)
    (tmp_path / "indexes" / "qdrant" / "meta.json").write_text("{}", encoding="utf-8")
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "m.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".hidden").write_text("", encoding="utf-8")

    listing = workspace_file_listing(tmp_path)
    assert "- prompt_principles.md (13 bytes)" in listing
    assert "- files/notes.txt (1 bytes)" in listing
    assert "qdrant" not in listing and "m.json" not in listing and ".hidden" not in listing


def test_listing_is_capped_and_says_how_many_more(tmp_path):
    for i in range(7):
        (tmp_path / f"f{i}.txt").write_text("", encoding="utf-8")
    listing = workspace_file_listing(tmp_path, limit=5)
    assert listing.count("\n- f") == 5
    assert "... and 2 more" in listing


def test_empty_workspace_says_so(tmp_path):
    assert "empty" in workspace_file_listing(tmp_path)


def test_file_list_rides_on_the_turn_only_for_a_project_workspace(tmp_path):
    (tmp_path / "a.md").write_text("", encoding="utf-8")
    orch = AgentOrchestrator.__new__(AgentOrchestrator)
    orch.memory_store = object()  # no _session_factory: no profile lines
    assert "a.md" in orch._space_context("WORKSPACE", "p1", tmp_path)
    assert "a.md" not in orch._space_context("WORKSPACE", None, tmp_path)
    assert "a.md" not in orch._space_context("FREEFORM", "p1", tmp_path)


# --- 4c: tool results in history ---------------------------------------------------------------

def _call(cid, name="read_file", args=None):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": args or {"file_path": "a.md"}}}


def test_stored_tool_result_replays_as_a_call_and_result_pair(store):
    store.append_message("s", role="user", content="what's in a.md?")
    store.append_message("s", role="tool", content="# Principles", name="read_file", tool_calls=[_call("call_1")])
    store.append_message("s", role="assistant", content="It lists the principles.")

    pkg = ContextManager(memory_store=store).build_context(session_id="s", user_message="now shorten it")
    history = [m for m in pkg.messages if m["role"] != "system"]
    assert [m["role"] for m in history] == ["user", "assistant", "tool", "assistant", "user"]
    assert history[1]["tool_calls"][0]["id"] == "call_1"
    assert history[2] == {"role": "tool", "tool_call_id": "call_1", "name": "read_file", "content": "# Principles"}


def test_tool_row_without_its_call_is_not_replayed(store):
    """Rows stored before calls were kept can't be paired; a bare tool message breaks the template."""
    store.append_message("s", role="user", content="hi")
    store.append_message("s", role="tool", content="orphan", name="read_file")
    store.append_message("s", role="assistant", content="hello")
    pkg = ContextManager(memory_store=store).build_context(session_id="s", user_message="again")
    assert all(m["role"] != "tool" for m in pkg.messages)


def test_history_is_the_newest_turns_and_tool_rows_dont_count(store, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "context_tier4_max_messages", 4)
    for i in range(6):
        store.append_message("s", role="user", content=f"q{i}")
        store.append_message("s", role="tool", content=f"r{i}", name="read_file", tool_calls=[_call(f"c{i}")])
        store.append_message("s", role="assistant", content=f"a{i}")

    pkg = ContextManager(memory_store=store).build_context(session_id="s", user_message="next")
    spoken = [m["content"] for m in pkg.messages if m["role"] in ("user", "assistant") and m["content"]]
    # The newest four user/assistant rows (the opening turns were what used to come back).
    assert spoken[:4] == ["q4", "a4", "q5", "a5"]
    assert [m["tool_call_id"] for m in pkg.messages if m["role"] == "tool"] == ["c4", "c5"]


def test_get_messages_limit_returns_the_newest_in_order(store):
    for i in range(5):
        store.append_message("s", role="user", content=str(i))
    assert [m["content"] for m in store.get_messages("s", limit=2)] == ["3", "4"]

    from app.memory.ephemeral import EphemeralRegistry
    eph = EphemeralMemoryStore(store, registry=EphemeralRegistry())
    eph.get_or_create_session("e", chat_mode="WORKSPACE", project_id=None)
    for i in range(5):
        eph.append_message("e", role="user", content=str(i))
    assert [m["content"] for m in eph.get_messages("e", limit=2)] == ["3", "4"]


def test_persisted_tool_result_is_capped(store):
    orch = AgentOrchestrator.__new__(AgentOrchestrator)
    orch.memory_store = store
    big = "x" * (TOOL_HISTORY_MAX_CHARS + 500)
    orch._persist_tool_result("s", "call_9", "read_file", {"file_path": "big.md"}, big)
    row = store.get_messages("s")[0]
    assert row["role"] == "tool" and row["name"] == "read_file"
    assert row["content"].startswith("x" * TOOL_HISTORY_MAX_CHARS)
    assert "500 more chars" in row["content"]
    assert row["tool_calls"][0]["function"]["arguments"] == {"file_path": "big.md"}


# --- 4d: one Qdrant client per index folder ----------------------------------------------------

def test_two_vector_stores_share_one_client_per_project(tmp_path):
    from app.rag.chunker import DocumentChunkModel
    from app.rag.embeddings import EmbeddingService
    from app.rag.vector_store import VectorStoreService
    from datetime import datetime

    svc = EmbeddingService(device="cpu", embedding_dim=16, use_sidecar=False)
    indexer_side = VectorStoreService(workspace_root=tmp_path, embedding_service=svc)
    retriever_side = VectorStoreService(workspace_root=tmp_path, embedding_service=svc)
    try:
        chunk = DocumentChunkModel(
            id="chunk-1", document_id="doc-1", project_id="p1", file_path="a.md", file_name="a.md",
            chunk_index=0, start_line=1, end_line=1, content="prompt principles",
            language="markdown", modified_timestamp=datetime.now(),
        )
        vec = svc.embed(["prompt principles"])[0]
        # The second store opening the same folder used to raise "already accessed".
        assert retriever_side.get_client("p1")[0] is indexer_side.get_client("p1")[0]
        assert indexer_side.upsert_chunks("p1", [chunk], [vec]) == 1
        hits = retriever_side.search_semantic("p1", vec, top_k=1)
        assert hits and hits[0]["chunk_id"] == "chunk-1"
    finally:
        indexer_side.delete_project_index("p1")
