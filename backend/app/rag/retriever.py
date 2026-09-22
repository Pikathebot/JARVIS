import logging
import re
import threading
from typing import Any, Optional

from app.config import settings
from app.rag.embeddings import EmbeddingService, RerankerService, _query_terms
from app.rag.vector_store import VectorStoreService
from app.rag.keyword_store import KeywordSearchService

logger = logging.getLogger("jarvis.rag.retriever")

# A short command aimed at the machine is not a question about the workspace. "delete
# probe.txt" used to pull 5 KB of repo code about deleting attachments into the turn because
# retrieval ran on every WORKSPACE message and something always ranks first. These are the
# verbs the OS/file tools act on; the length cap keeps "delete the function that handles
# attachment uploads" (a request that needs the code) on the retrieval path.
_IMPERATIVE_VERBS = frozenset("""
delete remove open launch start run execute kill close stop play pause mute unmute set copy paste
focus switch turn press restart reboot shutdown lock notify remind volume type click minimize
maximize
""".split())
_LEADING_FILLER = frozenset("please jarvis hey ok okay can could would you now just quickly".split())
_CHITCHAT = frozenset((
    "hi", "hello", "hey", "sup", "yo", "greetings", "thanks", "thank you", "ok", "okay", "cool",
    "nice", "great", "bye", "goodbye", "good morning", "good evening", "good night",
    "how are you", "who are you", "what are you", "help",
))
_QUESTION_WORDS = frozenset("how what why which where who whose explain describe about".split())
_MAX_IMPERATIVE_WORDS = 6


def should_retrieve(query: str) -> bool:
    """False for turns that cannot benefit from workspace context: chit-chat, and short
    imperatives that name an action rather than ask about anything."""
    q = (query or "").strip().lower()
    if len(q) < 4:
        return False
    words = re.findall(r"[a-z0-9_.'/\\-]+", q)
    if not words:
        return False
    if " ".join(words) in _CHITCHAT or q.rstrip("!?.") in _CHITCHAT:
        return False
    if not _query_terms(q):  # "why?", "what is it?": nothing to look for
        return False
    body = list(words)
    while body and body[0] in _LEADING_FILLER:
        body.pop(0)
    if (
        body
        and body[0] in _IMPERATIVE_VERBS
        and len(body) <= _MAX_IMPERATIVE_WORDS
        and not any(w in _QUESTION_WORDS for w in body[1:])  # "open questions about the monitor"
    ):
        return False
    return True


def reciprocal_rank_fusion(
    ranked_lists: list[list[dict[str, Any]]],
    k: int = 60
) -> list[dict[str, Any]]:
    """
    Reciprocal Rank Fusion (RRF) algorithm.
    Combines multiple ranked candidate lists into a single unified ranking.
    RRF_score(d) = sum_m( 1 / (k + rank_m(d)) )
    """
    scores: dict[str, float] = {}
    doc_map: dict[str, dict[str, Any]] = {}

    for ranked_list in ranked_lists:
        for rank, doc in enumerate(ranked_list, start=1):
            doc_id = doc.get("chunk_id") or doc.get("id")
            if not doc_id:
                continue

            if doc_id not in doc_map:
                doc_map[doc_id] = dict(doc)

            # Accumulate RRF score
            rrf_delta = 1.0 / (k + rank)
            scores[doc_id] = scores.get(doc_id, 0.0) + rrf_delta

    # Sort merged results by RRF score descending
    sorted_doc_ids = sorted(scores.keys(), key=lambda d_id: scores[d_id], reverse=True)

    fused_results = []
    for rank, doc_id in enumerate(sorted_doc_ids, start=1):
        doc = doc_map[doc_id]
        doc["rrf_score"] = round(scores[doc_id], 6)
        doc["rrf_rank"] = rank
        fused_results.append(doc)

    return fused_results


class HybridRetriever:
    """
    Hybrid Retrieval Engine complying with Build Plan Section 9.
    Orchestrates Semantic Vector Search (Qdrant), Exact Keyword/Symbol Search (FTS5),
    Reciprocal Rank Fusion (RRF), and CPU-Only Cross-Encoder Reranking.
    """

    def __init__(
        self,
        embedding_service: Optional[EmbeddingService] = None,
        vector_store: Optional[VectorStoreService] = None,
        keyword_store: Optional[KeywordSearchService] = None,
        reranker: Optional[RerankerService] = None
    ):
        self.embedding_service = embedding_service or EmbeddingService()
        self.vector_store = vector_store or VectorStoreService(embedding_service=self.embedding_service)
        self.keyword_store = keyword_store or KeywordSearchService()
        self.reranker = reranker or RerankerService()
        self._reindexing: set[str] = set()

    def _reindex_in_background(self, project_id: str) -> None:
        """One rebuild per project per process; the indexer wipes the stale space first."""
        if project_id in self._reindexing:
            return
        self._reindexing.add(project_id)
        logger.info("Project %s index is from another embedding space; rebuilding in the background.", project_id)

        def _run() -> None:
            try:
                from app.rag.indexer import WorkspaceIndexer
                WorkspaceIndexer(
                    embedding_service=self.embedding_service, vector_store=self.vector_store,
                    keyword_store=self.keyword_store,
                ).index_project(project_id)
            except Exception as e:
                logger.warning("Background re-index of project %s failed: %s", project_id, e)
            finally:
                self._reindexing.discard(project_id)

        threading.Thread(target=_run, name=f"reindex-{project_id[:8]}", daemon=True).start()

    def retrieve(
        self,
        project_id: str,
        query: str,
        top_k: int = 5,
        semantic_limit: int = 20,
        keyword_limit: int = 20,
        filters: Optional[dict[str, Any]] = None,
        gate: bool = True,
    ) -> list[dict[str, Any]]:
        """
        Execute full hybrid retrieval pipeline:
        1. Embed user query on CPU.
        2. Semantic vector search (top 20 from Qdrant).
        3. Lexical exact keyword/symbol search (top 20 from SQLite FTS5).
        4. Reciprocal Rank Fusion (merge to top 40 candidates).
        5. CPU Reranking (top 5 final chunks with Section 8 metadata).
        """
        if not query or not query.strip():
            return []
        if gate and not should_retrieve(query):
            logger.debug("Skipping retrieval for %r: not a question about the workspace.", query)
            return []

        # An index built in another embedding space is noise against this query; rebuild it
        # in the background (once) and serve nothing rather than junk until it is ready.
        if not self.vector_store.generation_matches(project_id):
            self._reindex_in_background(project_id)
            return []

        # 1. Semantic search
        query_vector: list[float] = []
        try:
            query_vector = self.embedding_service.embed_query(query)
            semantic_results = self.vector_store.search_semantic(
                project_id=project_id,
                query_vector=query_vector,
                top_k=semantic_limit,
                filters=filters
            )
        except Exception as e:
            logger.warning("Semantic search failed for project %s: %s", project_id, e)
            semantic_results = []

        # 2. Keyword exact search
        try:
            keyword_results = self.keyword_store.search_keyword(
                project_id=project_id,
                query=query,
                top_k=keyword_limit
            )
        except Exception as e:
            logger.warning("Keyword search failed for project %s: %s", project_id, e)
            keyword_results = []

        # 3. Reciprocal Rank Fusion (RRF)
        fused_candidates = reciprocal_rank_fusion(
            [semantic_results, keyword_results],
            k=60
        )

        if not fused_candidates:
            logger.debug("No hybrid search candidates found for query: %s", query)
            return []

        # Take up to 40 candidates for CPU reranking
        rerank_candidates = fused_candidates[:40]

        # Every candidate gets the same relevance scale: the semantic hits already carry their
        # cosine; keyword-only hits get theirs from the vector stored for them at index time.
        if self.embedding_service.semantic and query_vector:
            missing = [c for c in rerank_candidates if c.get("similarity_score") is None]
            read_back = self.vector_store.similarities_for(
                project_id, [c.get("chunk_id") or c.get("id") for c in missing], query_vector
            ) if missing else {}
            for c in rerank_candidates:
                sim = c.get("similarity_score")
                if sim is None:
                    sim = read_back.get(c.get("chunk_id") or c.get("id"))
                if sim is not None:
                    c["semantic_score"] = float(sim)

        # 4. CPU-Only Reranking
        final_chunks = self.reranker.rerank(
            query=query,
            docs=rerank_candidates,
            top_k=top_k
        )

        logger.info(
            "Hybrid retrieval complete for project %s: %d semantic, %d keyword -> %d RRF -> %d final reranked chunks",
            project_id,
            len(semantic_results),
            len(keyword_results),
            len(fused_candidates),
            len(final_chunks)
        )
        return final_chunks
