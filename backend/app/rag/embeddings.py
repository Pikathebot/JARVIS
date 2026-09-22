import logging
import math
import hashlib
import re
from typing import Any, Optional
from app.config import settings

logger = logging.getLogger("jarvis.rag.embeddings")

# Words that carry no retrieval signal on their own. Deliberately small: the point is to stop
# "the"/"is"/"one" matching every chunk, not to do NLP.
_STOPWORDS = frozenset("""
a an and are as at be by can could did do does for from had has have how i if in into is it its
me my no not of on one or our say says so than that the their them then there these they this to
was we were what when where which who why will with would you your please thanks thank ok okay
""".split())


def _query_terms(query: str) -> list[str]:
    """Lowercased query tokens worth matching: alphanumeric, longer than two chars, not stopwords."""
    return [t for t in re.findall(r"[a-z0-9_]+", (query or "").lower()) if len(t) > 2 and t not in _STOPWORDS]


def cosine(a: list[float], b: list[float]) -> float:
    num = sum(x * y for x, y in zip(a, b))
    da = math.sqrt(sum(x * x for x in a))
    db = math.sqrt(sum(y * y for y in b))
    return num / (da * db) if da > 0 and db > 0 else 0.0


class EmbeddingService:
    """
    CPU-only embeddings. Real vectors come from the llama-server embedding sidecar
    (``app.rag.embedding_sidecar``); when it is disabled or has no model, a deterministic
    hashed bag-of-words vector stands in so indexing and search still function -- but those
    vectors carry no semantic signal, and the reranker's relevance floor knows to treat them
    as unscored. The two vector spaces are never mixed: ``identity`` names the space, and the
    indexer re-embeds a project whose index was built in a different one.
    """

    HASH_IDENTITY = "hashed-bow-v1"

    def __init__(
        self,
        model_name: Optional[str] = None,
        device: Optional[str] = None,
        embedding_dim: int = 768,
        sidecar: Optional[Any] = None,
        use_sidecar: Optional[bool] = None,
    ):
        self.device = (device or settings.rag_device).lower()
        self.embedding_dim = embedding_dim
        self._enforce_cpu_guard()
        # A caller that wants the hashed engine specifically (tests, a tiny dim) opts out; the
        # default follows the setting, and a sidecar that turns out to be unavailable degrades
        # to hashing on its own.
        self._use_sidecar = settings.rag_embedding_sidecar_enabled if use_sidecar is None else use_sidecar
        self._sidecar = sidecar
        self.model_name = model_name or (self.sidecar.model_path.name if self.sidecar else self.HASH_IDENTITY)
        logger.info(
            "RAG EmbeddingService initialized strictly on CPU (vectors=%s, VRAM allocated=0MB)", self.identity
        )

    def _enforce_cpu_guard(self) -> None:
        """RAG must never touch the 8 GB card: the sidecar launches with no GPU layers, and the
        fallback is pure Python."""
        assert self.device == "cpu", f"RAG models MUST run on CPU to protect 8GB GPU VRAM. Configured: {self.device}"

    @property
    def sidecar(self) -> Optional[Any]:
        if not self._use_sidecar:
            return None
        if self._sidecar is None:
            from app.rag.embedding_sidecar import get_embedding_sidecar
            self._sidecar = get_embedding_sidecar()
        return self._sidecar if self._sidecar.available else None

    @property
    def semantic(self) -> bool:
        """True when vectors come from a real model and cosine similarity means something."""
        return self.sidecar is not None

    @property
    def identity(self) -> str:
        """Names the vector space this service produces; stored beside each project's index."""
        sc = self.sidecar
        return sc.identity if sc is not None else f"{self.HASH_IDENTITY}:{self.embedding_dim}"

    def _generate_fast_cpu_vector(self, text: str) -> list[float]:
        """
        Deterministic, normalized float vector generator for CPU execution
        when heavyweight external model weights are offline.
        """
        dim = self.embedding_dim
        vec = [0.0] * dim
        words = text.lower().split()
        if not words:
            return vec

        for idx, word in enumerate(words):
            h = int(hashlib.sha256(word.encode("utf-8")).hexdigest(), 16)
            pos = h % dim
            weight = 1.0 / (math.log(idx + 2))
            sign = 1.0 if ((h >> 8) & 1) == 0 else -1.0
            vec[pos] += sign * weight

        # L2 normalize
        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 0.0:
            vec = [round(x / norm, 6) for x in vec]
        return vec

    def _embed(self, texts: list[str], prefix: str) -> list[list[float]]:
        if not texts:
            return []
        sc = self.sidecar
        if sc is not None:
            vectors = sc.embed([prefix + t for t in texts])
            if vectors is not None:
                return vectors
        return [self._generate_fast_cpu_vector(t) for t in texts]

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Document-side vectors for a batch of chunk texts."""
        return self._embed(texts, settings.rag_embedding_document_prefix)

    def embed_query(self, query: str) -> list[float]:
        """Query-side vector for a single search query."""
        results = self._embed([query], settings.rag_embedding_query_prefix)
        return results[0] if results else [0.0] * self.embedding_dim


class RerankerService:
    """
    CPU-only relevance scoring with a floor.

    There is no cross-encoder here (``sentence_transformers`` is not installed and a 0.6B
    reranker on the CPU would cost seconds per turn). The relevance signal is the embedding
    sidecar's cosine similarity between the query and each candidate -- the retriever fills
    ``semantic_score`` in for every candidate, keyword hits included -- plus a small bonus
    when a query term names the chunk's symbol or file. Two cut-offs then decide what reaches
    the prompt: nothing below ``RAG_MIN_RELEVANCE``, and nothing more than ``RAG_RELEVANCE_GAP``
    below the best chunk. A third looks at the shape: a question with an answer in the
    workspace peaks (the best chunk clears the sixth by ``RAG_MIN_PEAK`` or more), while a
    query with no target scores everything alike -- "what is the weather" against a code base
    is 0.55, 0.54, 0.54, 0.53... -- and then nothing is injected. Before this, "delete
    probe.txt" put 5 KB of unrelated repo code about deleting attachments into the turn
    because something always ranks first.

    Without a semantic score (hashed vectors) it falls back to lexical overlap, and there a
    chunk that shares no real term with the query is dropped rather than padded in by rank.
    """

    SYMBOL_BONUS = 0.05
    MAX_BONUS = 0.15

    def __init__(
        self,
        model_name: Optional[str] = None,
        device: Optional[str] = None,
        min_relevance: Optional[float] = None,
        relevance_gap: Optional[float] = None,
        min_peak: Optional[float] = None,
    ):
        self.model_name = model_name or "cosine+lexical"
        self.device = (device or settings.rag_device).lower()
        assert self.device == "cpu", f"RAG models MUST run on CPU to protect 8GB GPU VRAM. Configured: {self.device}"
        self.min_relevance = settings.rag_min_relevance if min_relevance is None else min_relevance
        self.relevance_gap = settings.rag_relevance_gap if relevance_gap is None else relevance_gap
        self.min_peak = settings.rag_min_peak if min_peak is None else min_peak
        logger.info(
            "RAG RerankerService initialized (floor=%.2f, gap=%.2f, peak=%.2f)",
            self.min_relevance, self.relevance_gap, self.min_peak,
        )

    @staticmethod
    def _lexical(query_words: set[str], doc: dict[str, Any]) -> tuple[float, int]:
        """(lexical score, number of query terms naming the symbol or file)."""
        content = (doc.get("content") or "").lower()
        symbol_name = (doc.get("symbol_name") or "").lower()
        file_path = (doc.get("file_path") or "").lower()
        score = 0.0
        named = 0
        for qw in query_words:
            hit = False
            if qw in symbol_name:
                score += 3.0
                hit = True
            if qw in file_path:
                score += 2.0
                hit = True
            if hit:
                named += 1
            if qw in content:
                score += 1.0 + min(content.count(qw) * 0.1, 1.0)
        return score, named

    def rerank(
        self,
        query: str,
        docs: list[dict[str, Any]],
        top_k: int = 5
    ) -> list[dict[str, Any]]:
        """
        Score candidates, apply the floor and the gap, return at most top_k by relevance.
        Each doc carries 'content', optional metadata, and -- when the retriever could measure
        it -- 'semantic_score' (cosine). The result's 'score' is the relevance used.
        """
        if not docs:
            return []

        query_words = set(_query_terms(query))
        scored: list[dict[str, Any]] = []
        for doc in docs:
            d = dict(doc)
            lex, named = self._lexical(query_words, d)
            semantic = d.get("semantic_score")
            if semantic is not None:
                relevance = float(semantic) + min(self.MAX_BONUS, self.SYMBOL_BONUS * named)
            else:
                # No semantic signal: lexical evidence or nothing.
                if lex <= 0.0:
                    continue
                relevance = lex + 1.0 / (d.get("initial_rank", 50) + 10)
            d["score"] = round(relevance, 4)
            d["lexical_score"] = round(lex, 4)
            scored.append(d)

        scored.sort(key=lambda x: x["score"], reverse=True)
        if not scored:
            return []

        if scored[0].get("semantic_score") is not None:
            best = scored[0]["score"]
            if len(scored) >= 6 and best - scored[5]["score"] < self.min_peak:
                logger.debug(
                    "Flat relevance (%.3f..%.3f over top 6) for %r: nothing in the workspace is about this.",
                    best, scored[5]["score"], query,
                )
                return []
            floor = max(self.min_relevance, best - self.relevance_gap)
            kept = [d for d in scored if d["score"] >= floor]
            if len(kept) < len(scored):
                logger.debug(
                    "Relevance floor %.2f (best %.2f) dropped %d of %d candidates for %r",
                    floor, best, len(scored) - len(kept), len(scored), query,
                )
            scored = kept
        return scored[:top_k]
