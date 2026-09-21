import pytest
from app.rag.embeddings import EmbeddingService, RerankerService


def test_embedding_service_cpu_initialization():
    service = EmbeddingService(device="cpu")
    assert service.device == "cpu"


def test_embedding_service_generates_dense_vectors():
    service = EmbeddingService(device="cpu", embedding_dim=128)
    texts = [
        "def calculate_trajectory(velocity, angle):\n    return velocity * math.sin(angle)",
        "class GameEngine:\n    def __init__(self):\n        self.running = True"
    ]
    embeddings = service.embed(texts)

    assert len(embeddings) == 2
    assert len(embeddings[0]) == 128
    assert len(embeddings[1]) == 128
    assert all(isinstance(val, float) for val in embeddings[0])

    # Test single query embedding
    query_vec = service.embed_query("calculate trajectory math")
    assert len(query_vec) == 128


def test_reranker_service_orders_candidates_by_relevance():
    reranker = RerankerService(device="cpu")
    query = "calculate trajectory"

    docs = [
        {"id": "doc1", "content": "class Recipe:\n    def bake_cake(self): pass", "symbol_name": "Recipe"},
        {"id": "doc2", "content": "def calculate_trajectory(v, a):\n    return v * a", "symbol_name": "calculate_trajectory"},
        {"id": "doc3", "content": "Unrelated documentation regarding database connections", "symbol_name": "DatabaseConn"},
    ]

    reranked = reranker.rerank(query=query, docs=docs, top_k=2)

    # Only the chunk with real term evidence comes back: the lexical scorer has no semantic
    # signal, so unrelated chunks are dropped rather than padded in to fill top_k.
    assert [d["id"] for d in reranked] == ["doc2"]
    assert reranked[0]["score"] > 0

    # Stopwords and short tokens are not evidence: a query of nothing but them matches nothing.
    assert reranker.rerank(query="why is it the one?", docs=docs, top_k=3) == []
