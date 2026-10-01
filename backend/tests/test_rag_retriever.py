import json
import pytest
from pathlib import Path
from sqlmodel import SQLModel, create_engine, Session, select
from sqlalchemy import text

from app.database.models import Project, Document, DocumentChunk
from app.rag.chunker import SyntaxAwareChunker, DocumentChunkModel
from app.rag.embeddings import EmbeddingService, RerankerService
from app.rag.vector_store import VectorStoreService
from app.rag.keyword_store import KeywordSearchService
from app.rag.retriever import HybridRetriever, reciprocal_rank_fusion
from app.rag.indexer import ProjectIndexer


@pytest.fixture
def rag_hybrid_env(tmp_path):
    """Isolated SQLite database with FTS5 and isolated workspace directory."""
    test_db = tmp_path / "test_hybrid.db"
    db_url = f"sqlite:///{test_db}"
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)

    # Initialize FTS5 table with tokenchars for symbols
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE VIRTUAL TABLE IF NOT EXISTS document_chunks_fts USING fts5(
                chunk_id UNINDEXED,
                project_id UNINDEXED,
                file_path,
                symbol_name,
                content,
                metadata_json UNINDEXED,
                tokenize = "unicode61 tokenchars '_:'"
            );
        """))
        conn.commit()

    def session_factory():
        return Session(engine)

    return session_factory, tmp_path


def test_reciprocal_rank_fusion_merges_rankings():
    list_a = [
        {"chunk_id": "chunk_1", "symbol_name": "SymA", "score": 0.95},
        {"chunk_id": "chunk_2", "symbol_name": "SymB", "score": 0.85},
    ]
    list_b = [
        {"chunk_id": "chunk_2", "symbol_name": "SymB", "score": 10.0},
        {"chunk_id": "chunk_3", "symbol_name": "SymC", "score": 5.0},
    ]

    fused = reciprocal_rank_fusion([list_a, list_b], k=60)

    assert len(fused) == 3
    # chunk_2 appeared in both lists (rank 2 in A, rank 1 in B), so it gets the highest cumulative RRF score
    assert fused[0]["chunk_id"] == "chunk_2"
    assert "rrf_score" in fused[0]
    assert fused[0]["rrf_score"] > fused[1]["rrf_score"]


def test_fts5_keyword_store_symbol_matching(rag_hybrid_env):
    session_factory, _ = rag_hybrid_env
    keyword_store = KeywordSearchService(session_factory=session_factory)

    chunks = [
        DocumentChunkModel(
            id="c1",
            project_id="proj_fts",
            file_path="Source/NexusCharacter.h",
            file_name="NexusCharacter.h",
            chunk_index=0,
            start_line=1,
            end_line=20,
            content="UCLASS()\nclass NEXUS_API ANexusCharacter : public AActor\n{\n    void FireWeapon();\n};",
            symbol_name="ANexusCharacter",
            symbol_type="class",
            language="cpp"
        ),
        DocumentChunkModel(
            id="c2",
            project_id="proj_fts",
            file_path="src/calc.py",
            file_name="calc.py",
            chunk_index=0,
            start_line=1,
            end_line=10,
            content="def compute_trajectory_path(velocity, angle):\n    return velocity * angle",
            symbol_name="compute_trajectory_path",
            symbol_type="function",
            language="python"
        ),
    ]

    indexed_count = keyword_store.index_chunks("proj_fts", chunks)
    assert indexed_count == 2

    # Exact C++ symbol search
    cpp_results = keyword_store.search_keyword("proj_fts", "ANexusCharacter", top_k=5)
    assert len(cpp_results) == 1
    assert cpp_results[0]["chunk_id"] == "c1"
    assert cpp_results[0]["symbol_name"] == "ANexusCharacter"

    # Exact Python snake_case search with underscores preserved by tokenizer
    py_results = keyword_store.search_keyword("proj_fts", "compute_trajectory_path", top_k=5)
    assert len(py_results) == 1
    assert py_results[0]["chunk_id"] == "c2"
    assert py_results[0]["symbol_name"] == "compute_trajectory_path"


def test_vector_store_local_qdrant_isolation(rag_hybrid_env):
    _, tmp_path = rag_hybrid_env
    emb_service = EmbeddingService(device="cpu", embedding_dim=64)
    vector_store = VectorStoreService(workspace_root=tmp_path, embedding_service=emb_service)

    chunks = [
        DocumentChunkModel(
            id="v1",
            project_id="proj_vec_test",
            file_path="src/game.py",
            file_name="game.py",
            chunk_index=0,
            start_line=1,
            end_line=15,
            content="class GameManager:\n    def run(self): pass",
            symbol_name="GameManager",
            symbol_type="class",
            language="python"
        )
    ]
    embeddings = emb_service.embed([chunks[0].content])

    upserted = vector_store.upsert_chunks("proj_vec_test", chunks, embeddings)
    assert upserted == 1

    # Verify directory isolation in workspace/projects/proj_vec_test/indexes/qdrant
    qdrant_path = tmp_path / "projects" / "proj_vec_test" / "indexes" / "qdrant"
    assert qdrant_path.exists()
    assert qdrant_path.is_dir()

    # Query semantic search
    query_vec = emb_service.embed_query("game engine loop")
    results = vector_store.search_semantic("proj_vec_test", query_vec, top_k=5)
    assert len(results) >= 1
    assert results[0]["chunk_id"] == "v1"
    assert results[0]["symbol_name"] == "GameManager"


def test_hybrid_retriever_end_to_end(rag_hybrid_env):
    session_factory, tmp_path = rag_hybrid_env

    # 1. Create project record
    with session_factory() as session:
        proj = Project(
            id="proj_hybrid_e2e",
            name="Hybrid E2E Test",
            workspace_path=str(tmp_path)
        )
        session.add(proj)
        session.commit()

    # 2. Setup project files
    files_dir = tmp_path / "projects" / "proj_hybrid_e2e" / "files"
    files_dir.mkdir(parents=True, exist_ok=True)

    ue_file = files_dir / "CombatCharacter.h"
    ue_file.write_text(
        "#include \"CoreMinimal.h\"\n\nUCLASS()\nclass NEXUS_API ACombatCharacter\n{\n    void LaunchMissile();\n};\n",
        encoding="utf-8"
    )

    py_file = files_dir / "physics.py"
    py_file.write_text(
        "def compute_trajectory(vel, ang):\n    \"\"\"Calculates projectile trajectory.\"\"\"\n    return vel * ang\n",
        encoding="utf-8"
    )

    # 3. Initialize components
    emb_service = EmbeddingService(device="cpu", embedding_dim=64)
    vector_store = VectorStoreService(workspace_root=tmp_path, embedding_service=emb_service)
    keyword_store = KeywordSearchService(session_factory=session_factory)
    reranker = RerankerService(device="cpu")

    indexer = ProjectIndexer(
        session_factory=session_factory,
        embedding_service=emb_service,
        vector_store=vector_store,
        keyword_store=keyword_store
    )

    # 4. Index project files
    summary = indexer.index_project("proj_hybrid_e2e")
    assert summary["indexed_files"] == 2
    assert summary["total_chunks"] >= 2

    # 5. Hybrid Retrieval
    retriever = HybridRetriever(
        embedding_service=emb_service,
        vector_store=vector_store,
        keyword_store=keyword_store,
        reranker=reranker
    )

    # Query for C++ symbol
    results_ue = retriever.retrieve(
        project_id="proj_hybrid_e2e",
        query="ACombatCharacter LaunchMissile",
        top_k=2
    )

    assert len(results_ue) >= 1
    assert "ACombatCharacter" in results_ue[0]["content"]
    assert results_ue[0]["symbol_name"] == "ACombatCharacter"
    assert "score" in results_ue[0]

    # Query for Python calculation
    results_py = retriever.retrieve(
        project_id="proj_hybrid_e2e",
        query="calculate projectile trajectory physics",
        top_k=2
    )

    assert len(results_py) >= 1
    assert "compute_trajectory" in results_py[0]["content"]


# --- the index follows edits and deletes (tool review 2026-09-30: stale chunks kept serving) ---

def _fts_rows(session_factory, project_id):
    with session_factory() as s:
        return s.execute(text("SELECT file_path, content FROM document_chunks_fts WHERE project_id = :p"),
                         {"p": project_id}).all()


def test_reindex_edit_delete_and_rescan_leave_no_stale_chunks(rag_hybrid_env):
    session_factory, tmp_path = rag_hybrid_env
    pid = "proj_stale"
    with session_factory() as session:
        session.add(Project(id=pid, name="Stale", workspace_path=str(tmp_path)))
        session.commit()
    files_dir = tmp_path / "files"
    files_dir.mkdir()
    a, b = files_dir / "a.py", files_dir / "b.py"
    a.write_text("def old_name_alpha():\n    return 1\n", encoding="utf-8")
    b.write_text("def beta():\n    return 2\n", encoding="utf-8")

    emb = EmbeddingService(device="cpu", embedding_dim=64)
    vectors = VectorStoreService(workspace_root=tmp_path, embedding_service=emb)
    keywords = KeywordSearchService(session_factory=session_factory)
    indexer = ProjectIndexer(session_factory=session_factory, embedding_service=emb,
                             vector_store=vectors, keyword_store=keywords)
    indexer.index_project(pid)
    points = lambda: vectors.get_client(pid)[0].count(vectors.get_client(pid)[1]).count

    # An edit replaces the file's chunks everywhere, not only in the DB.
    a.write_text("def new_name_alpha():\n    return 3\n", encoding="utf-8")
    indexer.index_file(a.resolve(), project_id=pid)
    contents = " ".join(c for _, c in _fts_rows(session_factory, pid))
    assert "new_name_alpha" in contents and "old_name_alpha" not in contents
    with session_factory() as s:
        db_chunks = len(s.exec(select(DocumentChunk)).all())
    assert points() == db_chunks == len(_fts_rows(session_factory, pid))

    # A deleted file leaves the index.
    a.unlink()
    assert indexer.remove_file(a, pid) >= 1
    assert all("a.py" not in p for p, _ in _fts_rows(session_factory, pid))
    assert indexer.remove_file(a, pid) == 0

    # A file deleted outside Jarvis is dropped on the next rescan.
    b.unlink()
    indexer.index_project(pid)
    assert _fts_rows(session_factory, pid) == [] and points() == 0
    with session_factory() as s:
        assert s.exec(select(Document).where(Document.project_id == pid)).all() == []
