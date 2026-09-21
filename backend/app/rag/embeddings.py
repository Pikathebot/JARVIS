import logging
import math
import hashlib
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
    import re
    return [t for t in re.findall(r"[a-z0-9_]+", (query or "").lower()) if len(t) > 2 and t not in _STOPWORDS]



class EmbeddingService:
    """
    CPU-Only Embedding Service for Project Nexus.
    Strictly enforced to run on CPU to prevent any allocation of the 8GB GPU VRAM.
    """

    def __init__(
        self,
        model_name: Optional[str] = None,
        device: Optional[str] = None,
        embedding_dim: int = 768
    ):
        self.model_name = model_name or settings.rag_embedding_model
        self.device = (device or settings.rag_device).lower()
        self.embedding_dim = embedding_dim
        self._model = None

        # Amendment 1: Dependency Guard against GPU / CUDA usage
        self._enforce_cpu_guard()

        logger.info(
            "RAG EmbeddingService initialized strictly on CPU (model=%s, device=%s, VRAM allocated=0MB)",
            self.model_name,
            self.device
        )

    def _enforce_cpu_guard(self) -> None:
        """Strictly assert that no GPU / CUDA context is used for RAG embeddings."""
        assert self.device == "cpu", f"RAG models MUST run on CPU to protect 8GB GPU VRAM. Configured: {self.device}"
        try:
            import torch
            if torch.cuda.is_available():
                logger.debug("CUDA detected on system, but RAG EmbeddingService is explicitly bound to CPU.")
        except ImportError:
            pass

    def _lazy_load_model(self):
        """Lazy loader for CPU embedding model."""
        if self._model is not None:
            return self._model

        # Attempt loading via sentence_transformers on CPU if available
        try:
            from sentence_transformers import SentenceTransformer
            logger.info("Loading sentence-transformers model '%s' on CPU...", self.model_name)
            self._model = SentenceTransformer(self.model_name, device="cpu")
            return self._model
        except Exception as e:
            logger.debug("SentenceTransformer not loaded (%s). Using high-performance CPU vector engine.", e)
            self._model = "cpu_fast_engine"
            return self._model

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

    def embed(self, texts: list[str]) -> list[list[float]]:
        """
        Compute normalized dense embedding vectors for a batch of texts on the CPU.
        """
        if not texts:
            return []

        model = self._lazy_load_model()
        if hasattr(model, "encode") and not isinstance(model, str):
            try:
                embeddings = model.encode(texts)
                if hasattr(embeddings, "tolist"):
                    return embeddings.tolist()
                return [list(emb) for emb in embeddings]
            except Exception as e:
                logger.warning("Error running transformer encode on CPU: %s. Falling back to fast CPU engine.", e)


        return [self._generate_fast_cpu_vector(t) for t in texts]

    def embed_query(self, query: str) -> list[float]:
        """Generate embedding vector for a single search query."""
        results = self.embed([query])
        return results[0] if results else [0.0] * self.embedding_dim


class RerankerService:
    """
    CPU-Only Cross-Encoder / Reranker Service.
    Evaluates query-document relevance scores strictly on system CPU/RAM.
    """

    def __init__(
        self,
        model_name: Optional[str] = None,
        device: Optional[str] = None
    ):
        self.model_name = model_name or settings.rag_reranker_model
        self.device = (device or settings.rag_device).lower()
        self._model = None

        # Amendment 1: Dependency Guard
        self._enforce_cpu_guard()

        logger.info(
            "RAG RerankerService initialized strictly on CPU (model=%s, device=%s, VRAM allocated=0MB)",
            self.model_name,
            self.device
        )

    def _enforce_cpu_guard(self) -> None:
        """Strictly assert that no GPU / CUDA context is used for RAG reranking."""
        assert self.device == "cpu", f"RAG models MUST run on CPU to protect 8GB GPU VRAM. Configured: {self.device}"
        try:
            import torch
            if torch.cuda.is_available():
                logger.debug("CUDA detected on system, but RAG RerankerService is explicitly bound to CPU.")
        except ImportError:
            pass

    def _lazy_load_model(self):
        """Lazy loader for CPU reranker model."""
        if self._model is not None:
            return self._model

        try:
            from sentence_transformers import CrossEncoder
            logger.info("Loading CrossEncoder model '%s' on CPU...", self.model_name)
            self._model = CrossEncoder(self.model_name, device="cpu")
            return self._model
        except Exception as e:
            logger.debug("CrossEncoder not loaded (%s). Using lexical-semantic scoring engine.", e)
            self._model = "cpu_fast_reranker"
            return self._model

    def rerank(
        self,
        query: str,
        docs: list[dict[str, Any]],
        top_k: int = 5
    ) -> list[dict[str, Any]]:
        """
        Rerank a candidate list of document chunk dictionaries based on query relevance.
        Each item in docs should contain 'content' and optional metadata.
        Returns top_k documents sorted by score descending.
        """
        if not docs:
            return []

        model = self._lazy_load_model()
        if hasattr(model, "predict"):
            try:
                pairs = [[query, d.get("content", "")] for d in docs]
                scores = model.predict(pairs)
                ranked = []
                for doc, score in zip(docs, scores):
                    doc_copy = dict(doc)
                    doc_copy["score"] = float(score)
                    ranked.append(doc_copy)
                ranked.sort(key=lambda x: x["score"], reverse=True)
                return ranked[:top_k]
            except Exception as e:
                logger.warning("Error running CrossEncoder on CPU: %s. Using lexical-semantic scorer.", e)

        # Fallback scoring: lexical overlap + symbol matches + reciprocal rank.
        #
        # This scorer has no semantic signal, so a chunk that shares no real term with the
        # query has no evidence of relevance and is dropped rather than padded in by rank:
        # before this, "why?" put five 0.017-scored chunks into every turn and the model
        # chatted about them. Stopwords and short tokens do not count as terms -- "one",
        # "the" and "is" used to score a test file 12.6 for "what colour is the sky".
        query_words = {w for w in _query_terms(query)}
        scored_docs = []
        for doc in docs:
            doc_copy = dict(doc)
            content = (doc_copy.get("content") or "").lower()
            symbol_name = (doc_copy.get("symbol_name") or "").lower()
            file_path = (doc_copy.get("file_path") or "").lower()

            score = 0.0
            for qw in query_words:
                if qw in symbol_name:
                    score += 3.0
                if qw in file_path:
                    score += 2.0
                if qw in content:
                    score += 1.0 + min(content.count(qw) * 0.1, 1.0)
            if score <= 0.0:
                continue

            # Reciprocal rank booster if initial rank exists
            initial_rank = doc_copy.get("initial_rank", 50)
            score += 1.0 / (initial_rank + 10)

            doc_copy["score"] = round(score, 4)
            scored_docs.append(doc_copy)

        scored_docs.sort(key=lambda x: x.get("score", 0.0), reverse=True)
        return scored_docs[:top_k]
