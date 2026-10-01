import json
import re
import logging
from typing import Any, Callable, Optional
from sqlalchemy import text
from sqlmodel import Session

from app.database import SessionLocal
from app.rag.chunker import DocumentChunkModel

logger = logging.getLogger("jarvis.rag.keyword_store")


class KeywordSearchService:
    """
    Portable Keyword and Exact Symbol Search Service.
    Built on top of SQLite FTS5 with 'tokenchars=\"_:\"' for code symbol recognition,
    structured so it can be swapped for PostgreSQL tsvector/GIN without touching the retriever.
    """

    def __init__(self, session_factory: Optional[Callable[[], Session]] = None):
        self._session_factory = session_factory or SessionLocal

    def _get_session(self) -> Session:
        return self._session_factory()

    def sanitize_fts_query(self, query: str) -> str:
        """
        Sanitize user query for SQLite FTS5 MATCH syntax while preserving code symbols,
        underscores, colons (C++ namespaces), and alphanumeric keywords.
        """
        if not query or not query.strip():
            return ""

        # Extract tokens that are alphanumeric or contain code identifier characters (_ and :)
        raw_tokens = re.findall(r'[A-Za-z0-9_:]+', query)
        if not raw_tokens:
            return ""

        # Quote each token to prevent SQLite FTS5 syntax errors (e.g. NEAR, AND, OR)
        quoted_tokens = [f'"{token}"' for token in raw_tokens]
        return " OR ".join(quoted_tokens)

    def index_chunks(self, project_id: str, chunks: list[DocumentChunkModel]) -> int:
        """
        Populate or update document chunks in the FTS5 virtual table.
        """
        if not chunks:
            return 0

        with self._get_session() as session:
            # Delete any existing FTS entries for these chunk IDs
            chunk_ids = [c.id for c in chunks]
            if chunk_ids:
                placeholders = ",".join(f":cid_{i}" for i in range(len(chunk_ids)))
                params = {f"cid_{i}": cid for i, cid in enumerate(chunk_ids)}
                session.execute(
                    text(f"DELETE FROM document_chunks_fts WHERE chunk_id IN ({placeholders})"),
                    params
                )

            # Insert new chunks
            insert_sql = text("""
                INSERT INTO document_chunks_fts (
                    chunk_id, project_id, file_path, symbol_name, content, metadata_json
                ) VALUES (
                    :chunk_id, :project_id, :file_path, :symbol_name, :content, :metadata_json
                )
            """)

            for c in chunks:
                metadata_payload = {
                    "chunk_id": c.id,
                    "project_id": project_id,
                    "document_id": c.document_id,
                    "file_path": c.file_path,
                    "file_name": c.file_name,
                    "chunk_index": c.chunk_index,
                    "start_line": c.start_line,
                    "end_line": c.end_line,
                    "symbol_name": c.symbol_name,
                    "symbol_type": c.symbol_type,
                    "namespace": c.namespace,
                    "imports": c.imports,
                    "language": c.language,
                    "git_commit_hash": c.git_commit_hash,
                    "modified_timestamp": c.modified_timestamp.isoformat(),
                }

                session.execute(
                    insert_sql,
                    {
                        "chunk_id": c.id,
                        "project_id": project_id,
                        "file_path": c.file_path,
                        "symbol_name": c.symbol_name or "",
                        "content": c.content,
                        "metadata_json": json.dumps(metadata_payload),
                    }
                )

            session.commit()

        logger.debug("Indexed %d chunks into document_chunks_fts for project %s", len(chunks), project_id)
        return len(chunks)

    def search_keyword(
        self,
        project_id: str,
        query: str,
        top_k: int = 20
    ) -> list[dict[str, Any]]:
        """
        Execute exact symbol and keyword search against SQLite FTS5 using BM25 rank.
        Returns top_k matching chunks with rank score and Section 8 metadata.
        """
        fts_query = self.sanitize_fts_query(query)
        if not fts_query:
            return []

        search_sql = text("""
            SELECT 
                chunk_id,
                project_id,
                file_path,
                symbol_name,
                content,
                metadata_json,
                bm25(document_chunks_fts) as rank_score
            FROM document_chunks_fts
            WHERE project_id = :project_id AND document_chunks_fts MATCH :query
            ORDER BY rank_score ASC
            LIMIT :top_k
        """)

        matched_chunks: list[dict[str, Any]] = []

        try:
            with self._get_session() as session:
                rows = session.execute(
                    search_sql,
                    {
                        "project_id": project_id,
                        "query": fts_query,
                        "top_k": top_k
                    }
                ).fetchall()

                for rank, row in enumerate(rows, start=1):
                    meta_raw = row[5]
                    payload = json.loads(meta_raw) if meta_raw else {}
                    payload.update({
                        "chunk_id": row[0],
                        "project_id": row[1],
                        "file_path": row[2],
                        "symbol_name": row[3],
                        "content": row[4],
                        "keyword_bm25_rank": rank,
                        "keyword_score": float(row[6]),
                    })
                    matched_chunks.append(payload)

        except Exception as e:
            logger.warning("FTS5 keyword search encountered error for project %s (%s): %s", project_id, fts_query, e)

        return matched_chunks

    def chunks_containing(self, project_id: str, needle: str, limit: int = 50) -> list[dict[str, Any]]:
        """Every indexed chunk whose text contains ``needle`` (case-insensitive substring, not an
        FTS match) -- the RAG trace's "was this name ever indexed, and where" check."""
        if not needle or not needle.strip():
            return []
        sql = text("""
            SELECT chunk_id, file_path, content, metadata_json FROM document_chunks_fts
            WHERE project_id = :project_id AND content LIKE :pattern LIMIT :limit
        """)
        with self._get_session() as session:
            rows = session.execute(
                sql, {"project_id": project_id, "pattern": f"%{needle.strip()}%", "limit": limit}
            ).fetchall()
        found = []
        for row in rows:
            meta = json.loads(row[3]) if row[3] else {}
            found.append({**meta, "chunk_id": row[0], "file_path": row[1], "content": row[2]})
        return found

    def delete_chunks(self, project_id: str, chunk_ids: list[str]) -> None:
        """Remove specific chunks from the FTS index (a file being un-indexed)."""
        if not chunk_ids:
            return
        with self._get_session() as session:
            for chunk_id in chunk_ids:
                session.execute(
                    text("DELETE FROM document_chunks_fts WHERE project_id = :project_id AND chunk_id = :chunk_id"),
                    {"project_id": project_id, "chunk_id": chunk_id},
                )
            session.commit()

    def delete_project_index(self, project_id: str) -> None:
        """Purge all FTS5 index records for a given project."""
        try:
            with self._get_session() as session:
                session.execute(
                    text("DELETE FROM document_chunks_fts WHERE project_id = :project_id"),
                    {"project_id": project_id}
                )
                session.commit()
            logger.info("Purged FTS5 index records for project %s", project_id)
        except Exception as e:
            logger.warning("Error purging FTS5 records for project %s: %s", project_id, e)
