"""Retrieval: chunking, embeddings, the per-project vector and keyword indexes, hybrid retrieval.

The names below resolve lazily (PEP 562). Importing them eagerly here meant any import of
``app.rag.embeddings`` -- the memory manager's, at backend start -- also pulled in the vector
store and with it ``qdrant_client``: 1.9 s of the backend's ~5 s of imports, for a feature that
is only used once a workspace turn retrieves or something is indexed.
"""
from importlib import import_module
from typing import Any

_EXPORTS = {
    "EmbeddingService": "app.rag.embeddings",
    "RerankerService": "app.rag.embeddings",
    "SyntaxAwareChunker": "app.rag.chunker",
    "DocumentChunkModel": "app.rag.chunker",
    "ProjectIndexer": "app.rag.indexer",
    "VectorStoreService": "app.rag.vector_store",
    "KeywordSearchService": "app.rag.keyword_store",
    "HybridRetriever": "app.rag.retriever",
    "reciprocal_rank_fusion": "app.rag.retriever",
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(module), name)
