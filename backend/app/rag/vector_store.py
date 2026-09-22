import shutil
import logging
from pathlib import Path
from typing import Any, Optional
from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from app.config import settings
from app.rag.chunker import DocumentChunkModel
from app.rag.embeddings import EmbeddingService

logger = logging.getLogger("jarvis.rag.vector_store")


class VectorStoreService:
    """
    Local On-Disk Vector Database Service using Qdrant (local mode).
    Strictly stores vector indices inside workspace/projects/{project_id}/indexes/qdrant.
    Zero GPU VRAM allocation.
    """

    def __init__(
        self,
        workspace_root: Optional[Path] = None,
        embedding_service: Optional[EmbeddingService] = None
    ):
        self.workspace_root = Path(workspace_root or settings.workspace_path).resolve()
        self.embedding_service = embedding_service or EmbeddingService()
        self._clients: dict[str, QdrantClient] = {}
        self._probed_dim: Optional[int] = None

    def _get_project_index_dir(self, project_id: str) -> Path:
        """Resolve workspace/projects/{project_id}/indexes/qdrant."""
        qdrant_dir = self.workspace_root / "projects" / project_id / "indexes" / "qdrant"
        qdrant_dir.mkdir(parents=True, exist_ok=True)
        return qdrant_dir

    def _probe_embedding_dimension(self) -> int:
        """
        Dynamically probe the embedding dimension from the CPU EmbeddingService (Amendment 1).
        Guarantees Qdrant vector size matches model output without hardcoding.
        """
        if self._probed_dim is not None:
            return self._probed_dim

        probe_vec = self.embedding_service.embed(["dimension probe"])[0]
        self._probed_dim = len(probe_vec)
        logger.info("Qdrant collection dimension dynamically probed: %d", self._probed_dim)
        return self._probed_dim

    def get_client(self, project_id: str) -> tuple[QdrantClient, str]:
        """
        Retrieve or initialize a local Qdrant client instance bound to a project's workspace directory.
        """
        collection_name = f"project_{project_id}_chunks"

        if project_id not in self._clients:
            qdrant_path = self._get_project_index_dir(project_id)
            client = QdrantClient(path=str(qdrant_path))
            dim = self._probe_embedding_dimension()

            # Ensure collection exists
            existing = [c.name for c in client.get_collections().collections]
            if collection_name not in existing:
                client.create_collection(
                    collection_name=collection_name,
                    vectors_config=qmodels.VectorParams(
                        size=dim,
                        distance=qmodels.Distance.COSINE
                    )
                )
                logger.info("Created local Qdrant collection '%s' (dim=%d)", collection_name, dim)

            self._clients[project_id] = client

        return self._clients[project_id], collection_name

    def _ensure_valid_uuid(self, val: str) -> str:
        """Ensure point ID is a valid RFC 4122 UUID string for Qdrant."""
        try:
            import uuid
            return str(uuid.UUID(str(val)))
        except (ValueError, AttributeError):
            import uuid
            return str(uuid.uuid5(uuid.NAMESPACE_DNS, str(val)))

    def upsert_chunks(
        self,
        project_id: str,
        chunks: list[DocumentChunkModel],
        embeddings: list[list[float]]
    ) -> int:
        """
        Upsert a batch of document chunks and their CPU embeddings into Qdrant.
        """
        if not chunks or not embeddings:
            return 0

        client, collection_name = self.get_client(project_id)
        points = []

        for chunk, emb in zip(chunks, embeddings):
            payload = {
                "chunk_id": chunk.id,
                "project_id": project_id,
                "document_id": chunk.document_id,
                "file_path": chunk.file_path,
                "file_name": chunk.file_name,
                "chunk_index": chunk.chunk_index,
                "start_line": chunk.start_line,
                "end_line": chunk.end_line,
                "symbol_name": chunk.symbol_name,
                "symbol_type": chunk.symbol_type,
                "namespace": chunk.namespace,
                "imports": chunk.imports,
                "language": chunk.language,
                "git_commit_hash": chunk.git_commit_hash,
                "modified_timestamp": chunk.modified_timestamp.isoformat(),
                "content": chunk.content,
            }

            points.append(
                qmodels.PointStruct(
                    id=self._ensure_valid_uuid(chunk.id),
                    vector=emb,
                    payload=payload
                )
            )


        client.upsert(
            collection_name=collection_name,
            points=points,
            wait=True
        )

        logger.debug("Upserted %d vector points to Qdrant collection '%s'", len(points), collection_name)
        return len(points)

    def search_semantic(
        self,
        project_id: str,
        query_vector: list[float],
        top_k: int = 20,
        filters: Optional[dict[str, Any]] = None
    ) -> list[dict[str, Any]]:
        """
        Query Qdrant for semantic matches using cosine similarity.
        Returns top_k matching chunks with similarity scores.
        """
        client, collection_name = self.get_client(project_id)

        qdrant_filter = None
        if filters:
            must_clauses = []
            for k, v in filters.items():
                must_clauses.append(
                    qmodels.FieldCondition(
                        key=k,
                        match=qmodels.MatchValue(value=v)
                    )
                )
            if must_clauses:
                qdrant_filter = qmodels.Filter(must=must_clauses)

        results = client.query_points(
            collection_name=collection_name,
            query=query_vector,
            limit=top_k,
            query_filter=qdrant_filter,
            with_payload=True
        ).points

        matched_chunks: list[dict[str, Any]] = []
        for rank, res in enumerate(results, start=1):
            payload = dict(res.payload or {})
            payload["similarity_score"] = float(res.score)
            payload["semantic_rank"] = rank
            matched_chunks.append(payload)

        return matched_chunks

    def similarities_for(
        self,
        project_id: str,
        chunk_ids: list[str],
        query_vector: list[float],
    ) -> dict[str, float]:
        """
        Cosine similarity between the query and the *stored* vector of each chunk, keyed by
        chunk id. Keyword hits that were not in the semantic top-k still have a vector on disk;
        reading it back is far cheaper than embedding the chunk again, and it gives every
        candidate the same relevance scale so the floor applies to all of them.
        """
        if not chunk_ids or not query_vector:
            return {}
        from app.rag.embeddings import cosine

        client, collection_name = self.get_client(project_id)
        by_point = {self._ensure_valid_uuid(c): c for c in chunk_ids}
        try:
            points = client.retrieve(
                collection_name=collection_name,
                ids=list(by_point.keys()),
                with_payload=False,
                with_vectors=True,
            )
        except Exception as e:
            logger.debug("Vector read-back failed for project %s: %s", project_id, e)
            return {}
        out: dict[str, float] = {}
        for pt in points:
            vec = pt.vector
            if isinstance(vec, dict):  # named vectors; we only ever store the default one
                vec = next(iter(vec.values()), None)
            if vec is None:
                continue
            out[by_point.get(str(pt.id), str(pt.id))] = cosine(query_vector, list(vec))
        return out

    # --------------------------------------------------------- generation

    def _generation_marker(self, project_id: str) -> Path:
        return self.workspace_root / "projects" / project_id / "indexes" / "embedding_model.txt"

    def generation_matches(self, project_id: str) -> bool:
        """
        Whether this project's vectors were produced by the embedding service in use now. A
        different model is a different space: comparing a nomic query against hashed-bag-of-
        words vectors yields noise, and Qdrant cannot tell as long as the dimensions agree.
        A project with no marker (indexed before markers existed, or never) does not match.
        """
        try:
            marker = self._generation_marker(project_id)
            return marker.is_file() and marker.read_text(encoding="utf-8").strip() == self.embedding_service.identity
        except Exception:
            return False

    def mark_generation(self, project_id: str) -> None:
        marker = self._generation_marker(project_id)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(self.embedding_service.identity, encoding="utf-8")

    def delete_chunks(self, project_id: str, chunk_ids: list[str]) -> None:
        """Remove specific points (a file being un-indexed)."""
        if not chunk_ids:
            return
        client, collection_name = self.get_client(project_id)
        client.delete(
            collection_name=collection_name,
            points_selector=qmodels.PointIdsList(points=[self._ensure_valid_uuid(c) for c in chunk_ids]),
        )

    def delete_project_index(self, project_id: str) -> None:
        """
        Close client and completely purge the on-disk Qdrant storage for a project.
        """
        if project_id in self._clients:
            client = self._clients.pop(project_id)
            try:
                client.close()
            except Exception:
                pass

        qdrant_dir = self.workspace_root / "projects" / project_id / "indexes" / "qdrant"
        if qdrant_dir.exists():
            shutil.rmtree(qdrant_dir, ignore_errors=True)
            logger.info("Purged local Qdrant directory for project %s", project_id)
        try:
            self._generation_marker(project_id).unlink(missing_ok=True)
        except Exception:
            pass
