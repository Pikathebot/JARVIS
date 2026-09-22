"""
The RAG relevance floor. Workspace context used to be injected on every WORKSPACE turn
because something always ranks first and the only scorer was lexical. Now: short imperatives
skip retrieval outright, every candidate carries a real cosine score, chunks below the floor
(or too far below the best) never reach the prompt, and an index built in another embedding
space is rebuilt rather than compared against.
"""
from pathlib import Path

import pytest

from app.rag.embeddings import EmbeddingService, RerankerService
from app.rag.retriever import should_retrieve
from app.rag.vector_store import VectorStoreService


# --- gate -----------------------------------------------------------------------------------

@pytest.mark.parametrize("query", [
    "delete probe.txt", "open discord", "set volume to 50", "run the tests", "please open the settings",
    "jarvis kill chrome", "hi", "Hello!", "thanks", "ok",
])
def test_short_imperatives_and_chitchat_skip_retrieval(query):
    assert should_retrieve(query) is False


@pytest.mark.parametrize("query", [
    "what does the governor do",
    "how are files split into chunks",
    "delete the function that handles attachment uploads",
    "explain the tool pipeline",
    "open questions about the awareness monitor",
])
def test_questions_and_long_requests_are_retrieved(query):
    assert should_retrieve(query) is True


# --- floor ----------------------------------------------------------------------------------

def _doc(chunk_id: str, semantic: float, symbol: str = "", path: str = "x.py") -> dict:
    return {"chunk_id": chunk_id, "content": f"body of {chunk_id}", "symbol_name": symbol,
            "file_path": path, "semantic_score": semantic}


def test_floor_and_gap_cut_unrelated_chunks():
    rr = RerankerService(device="cpu", min_relevance=0.55, relevance_gap=0.15)
    docs = [_doc("a", 0.78), _doc("b", 0.66), _doc("c", 0.60), _doc("d", 0.52), _doc("e", 0.40)]
    kept = rr.rerank("how does chunking work", docs, top_k=5)
    # d and e are under the 0.55 floor; c is under best-gap (0.63).
    assert [d["chunk_id"] for d in kept] == ["a", "b"]


def test_nothing_survives_when_the_best_is_below_the_floor():
    rr = RerankerService(device="cpu", min_relevance=0.55, relevance_gap=0.15)
    assert rr.rerank("delete probe.txt", [_doc("a", 0.53), _doc("b", 0.50)], top_k=5) == []


def test_symbol_match_bonus_lifts_a_named_chunk_over_the_floor():
    rr = RerankerService(device="cpu", min_relevance=0.55, relevance_gap=0.15)
    named = _doc("a", 0.52, symbol="delete_attachment", path="routers/attachments.py")
    kept = rr.rerank("where is delete_attachment defined", [named, _doc("b", 0.45)], top_k=5)
    assert [d["chunk_id"] for d in kept] == ["a"]
    assert kept[0]["score"] == pytest.approx(0.52 + 0.05, abs=1e-6)


def test_without_semantic_scores_lexical_overlap_still_gates():
    rr = RerankerService(device="cpu")
    docs = [{"chunk_id": "a", "content": "def calculate_trajectory(): ...", "symbol_name": "calculate_trajectory"},
            {"chunk_id": "b", "content": "nothing in common here", "symbol_name": ""}]
    kept = rr.rerank("calculate trajectory", docs, top_k=5)
    assert [d["chunk_id"] for d in kept] == ["a"]


# --- generation marker ----------------------------------------------------------------------

def test_index_generation_marker_tracks_the_embedding_identity(tmp_path):
    svc = EmbeddingService(device="cpu", embedding_dim=64, use_sidecar=False)
    store = VectorStoreService(workspace_root=tmp_path, embedding_service=svc)
    assert store.generation_matches("p1") is False
    store.mark_generation("p1")
    assert store.generation_matches("p1") is True
    assert (tmp_path / "projects" / "p1" / "indexes" / "embedding_model.txt").read_text() == svc.identity

    other = VectorStoreService(workspace_root=tmp_path,
                               embedding_service=EmbeddingService(device="cpu", embedding_dim=32, use_sidecar=False))
    assert other.generation_matches("p1") is False
    other.delete_project_index("p1")
    assert store.generation_matches("p1") is False


# --- sidecar (only where the model is installed) --------------------------------------------

@pytest.mark.skipif(
    not (Path(__file__).resolve().parents[2] / "models" / "embeddings" / "nomic-embed-text-v1.5.Q8_0.gguf").is_file(),
    reason="embedding GGUF not installed",
)
def test_embedding_sidecar_produces_vectors_with_real_separation(monkeypatch):
    from app.config import settings
    from app.rag.embedding_sidecar import EmbeddingSidecar
    from app.rag.embeddings import cosine

    monkeypatch.setattr(settings, "rag_embedding_sidecar_enabled", True)
    sidecar = EmbeddingSidecar(port=8013, idle_seconds=0)
    try:
        svc = EmbeddingService(device="cpu", sidecar=sidecar, use_sidecar=True)
        assert svc.semantic is True and svc.identity.endswith("nomic-embed-text-v1.5.Q8_0.gguf")
        docs = svc.embed([
            "class ResourceGovernor: polls GPU and VRAM telemetry and throttles requests",
            "def chunk_file(): split a source file into functions and markdown sections",
        ])
        q = svc.embed_query("how are files split into chunks")
        assert len(q) == 768
        assert cosine(q, docs[1]) > cosine(q, docs[0]) + 0.1
    finally:
        sidecar.stop()


def test_flat_scores_mean_no_target_and_nothing_is_injected():
    rr = RerankerService(device="cpu", min_relevance=0.62, relevance_gap=0.15, min_peak=0.04)
    # "what is the weather" against a code base: everything alike, best over the floor.
    flat = [_doc(str(i), 0.648 - 0.005 * i) for i in range(8)]
    assert rr.rerank("what is the weather", flat, top_k=5) == []
    # A real question peaks: same floor, but the sixth is well below the first.
    peaked = [_doc("a", 0.80), _doc("b", 0.78), _doc("c", 0.77), _doc("d", 0.76), _doc("e", 0.74), _doc("f", 0.73), _doc("g", 0.60)]
    assert [d["chunk_id"] for d in rr.rerank("what does the governor do", peaked, top_k=5)] == ["a", "b", "c", "d", "e"]


def test_no_content_terms_means_no_retrieval():
    assert should_retrieve("why?") is False
    assert should_retrieve("what is it?") is False
