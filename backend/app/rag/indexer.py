import os
import json
import uuid
import hashlib
import logging
from pathlib import Path
from datetime import datetime
from typing import Any, Callable, Optional, Union
from sqlmodel import Session, select

from app.config import settings
from app.database import SessionLocal
from app.database.models import Project, Document, DocumentChunk
from app.rag.chunker import SyntaxAwareChunker, DocumentChunkModel
from app.rag.embeddings import EmbeddingService
from app.rag.vector_store import VectorStoreService
from app.rag.keyword_store import KeywordSearchService

logger = logging.getLogger("jarvis.rag.indexer")

SUPPORTED_EXTENSIONS = {
    ".py", ".cpp", ".cxx", ".cc", ".c", ".h", ".hpp", ".cs",
    ".js", ".ts", ".tsx", ".jsx", ".json", ".yaml", ".yml",
    ".ini", ".md", ".txt", ".uproject", ".csv", ".html", ".css"
}


def looks_binary(content: str, sample: int = 4096) -> bool:
    """
    True when text read with errors="replace" is really a binary file: NUL bytes, or a heavy
    share of replacement/control characters in the first few KB. An image indexed as text
    becomes thousands of chunks of garbage that retrieval then pours into every turn.
    """
    head = content[:sample]
    if not head:
        return False
    if "\x00" in head:
        return True
    junk = sum(1 for ch in head if ch == "\ufffd" or (ord(ch) < 32 and ch not in "\t\n\r"))
    return junk / len(head) > 0.05


class ProjectIndexer:
    """
    Project Codebase & Knowledge Base Indexer complying with Build Plan Section 8.
    Scans project directories, computes SHA-256 hashes, parses syntax chunks,
    generates CPU embeddings, and persists metadata to SQLModel, Qdrant, and SQLite FTS5.
    """

    def __init__(
        self,
        session_factory: Optional[Callable[[], Session]] = None,
        chunker: Optional[SyntaxAwareChunker] = None,
        embedding_service: Optional[EmbeddingService] = None,
        vector_store: Optional[VectorStoreService] = None,
        keyword_store: Optional[KeywordSearchService] = None,
        workspace_root: Optional[Path] = None
    ):
        self._session_factory = session_factory
        self.chunker = chunker or SyntaxAwareChunker(
            chunk_size=settings.rag_chunk_size,
            chunk_overlap=settings.rag_chunk_overlap
        )
        self.embedding_service = embedding_service or EmbeddingService()
        self.vector_store = vector_store or VectorStoreService(
            workspace_root=workspace_root,
            embedding_service=self.embedding_service
        )
        self.keyword_store = keyword_store or KeywordSearchService(session_factory=self._get_session)
        self.workspace_root = workspace_root

    def ensure_embedding_generation(self, project_id: str) -> bool:
        """
        Make sure the project's index lives in the current embedding space. When it was built
        by a different embedding service (the hashed engine before the sidecar existed, or a
        model swap), everything indexed is discarded -- document rows, FTS rows and vectors --
        so the hash check below re-embeds every file on this pass instead of mixing spaces.
        Returns True when a wipe happened.
        """
        if self.vector_store is None or self.vector_store.generation_matches(project_id):
            return False
        logger.info(
            "Project %s index is from another embedding space; re-indexing with %s.",
            project_id, self.embedding_service.identity,
        )
        self.delete_project_index(project_id)
        self.vector_store.mark_generation(project_id)
        return True

    def _get_session(self) -> Session:
        if self._session_factory:
            return self._session_factory()
        from app.database.session import SessionLocal
        return SessionLocal()

    def compute_sha256(self, file_path: Path) -> str:
        """Compute SHA-256 hash of a local file."""
        hasher = hashlib.sha256()
        with open(file_path, "rb") as f:
            while chunk := f.read(65536):
                hasher.update(chunk)
        return hasher.hexdigest()

    def scan_project_paths(self, project_id: str) -> list[Path]:
        """
        Gathers all candidate source and knowledge files for indexing in a project.
        Scans workspace/projects/{project_id}/files, knowledge, and custom local_folders.
        """
        candidate_files: list[Path] = []
        workspace_base = Path(self.workspace_root or settings.workspace_path).resolve()

        # Check for project specific workspace_path
        is_custom_workspace = False
        try:
            with self._get_session() as session:
                project = session.get(Project, project_id)
                if project and project.workspace_path:
                    custom_wp = Path(project.workspace_path).resolve()
                    if custom_wp.exists() and custom_wp.is_dir():
                        workspace_base = custom_wp
                        is_custom_workspace = True
        except Exception as e:
            logger.debug("Error checking project %s workspace_path: %s", project_id, e)

        search_dirs: list[Path] = []
        if is_custom_workspace:
            # Custom project workspace: scan root folder directly
            search_dirs.append(workspace_base)
        else:
            project_dir = workspace_base / "projects" / project_id
            if not project_dir.exists() and (workspace_base / "files").exists():
                project_dir = workspace_base

            # 1. Project standard directories
            search_dirs.extend([
                project_dir / "files",
                project_dir / "knowledge",
            ])

        # 2. Check for configured local folders in database
        try:
            with self._get_session() as session:
                project = session.get(Project, project_id)
                if project and project.local_folders_json:
                    try:
                        custom_folders = json.loads(project.local_folders_json)
                        if isinstance(custom_folders, list):
                            for folder_str in custom_folders:
                                p = Path(folder_str).resolve()
                                if p.exists() and p.is_dir() and p not in search_dirs:
                                    search_dirs.append(p)
                    except Exception as json_err:
                        logger.debug("Error parsing local_folders_json for project %s: %s", project_id, json_err)
        except Exception as db_err:
            logger.debug("Error querying project %s for local folders: %s", project_id, db_err)

        EXCLUDED_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".next", "dist", "build", ".pytest_cache", "indexes", "qdrant"}
        seen_paths: set[Path] = set()

        for s_dir in search_dirs:
            if not s_dir.exists() or not s_dir.is_dir():
                continue
            for root, dirs, files in os.walk(s_dir):
                dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS and not d.startswith(".")]
                for f in files:
                    f_path = (Path(root) / f).resolve()
                    if f_path not in seen_paths and f_path.suffix.lower() in SUPPORTED_EXTENSIONS:
                        seen_paths.add(f_path)
                        candidate_files.append(f_path)

        return candidate_files

    def index_file(
        self,
        file_path: Path,
        project_id: str,
        git_commit_hash: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """
        Index a single file:
        - Check SHA-256 hash. If unchanged, skip.
        - If new or modified: chunk, embed on CPU, update DB, and return indexed chunk records.
        """
        if not file_path.exists() or not file_path.is_file():
            return []
        if file_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            # Uploads call this directly, bypassing the walker's extension filter: images,
            # PDFs and the like are stored as attachments, not chunked as text.
            logger.info("Not indexing %s: not a text type the indexer understands.", file_path.name)
            return []

        self.ensure_embedding_generation(project_id)
        rel_path_str = str(file_path)
        current_hash = self.compute_sha256(file_path)
        stat = file_path.stat()
        mtime = datetime.utcfromtimestamp(stat.st_mtime)
        size_bytes = stat.st_size

        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            logger.warning("Failed to read text from %s: %s", file_path, e)
            return []
        if looks_binary(content):
            logger.info("Not indexing %s: content looks binary.", file_path.name)
            return []

        # 1. Parse structured syntax chunks
        chunk_models: list[DocumentChunkModel] = self.chunker.chunk_file(
            file_path=rel_path_str,
            content=content,
            project_id=project_id,
            git_commit_hash=git_commit_hash,
            modified_timestamp=mtime
        )

        if not chunk_models:
            return []

        # 2. Generate CPU embeddings
        chunk_texts = [c.content for c in chunk_models]
        embeddings = self.embedding_service.embed(chunk_texts)

        indexed_records: list[dict[str, Any]] = []
        stale_ids: list[str] = []

        # 3. Database persistence
        with self._get_session() as session:
            # Check if Document already exists
            stmt = select(Document).where(
                Document.project_id == project_id,
                Document.file_path == rel_path_str
            )
            doc = session.exec(stmt).first()

            if doc:
                # If hash unchanged, return without re-writing
                if doc.file_hash == current_hash:
                    logger.debug("Skipping unchanged file %s (hash match)", rel_path_str)
                    return []

                # Remove old chunks -- from the stores too: retrieval reads Qdrant and FTS5
                # directly, so a chunk left there keeps serving the file's previous version.
                old_chunks = session.exec(
                    select(DocumentChunk).where(DocumentChunk.document_id == doc.id)
                ).all()
                for oc in old_chunks:
                    session.delete(oc)
                stale_ids = [oc.id for oc in old_chunks]

                doc.file_hash = current_hash
                doc.size_bytes = size_bytes
                doc.last_indexed_at = datetime.utcnow()
                doc.updated_at = datetime.utcnow()
                session.add(doc)
            else:
                doc = Document(
                    id=str(uuid.uuid4()),
                    project_id=project_id,
                    file_path=rel_path_str,
                    file_name=file_path.name,
                    language=self.chunker.detect_language(rel_path_str),
                    file_hash=current_hash,
                    size_bytes=size_bytes,
                    last_indexed_at=datetime.utcnow()
                )
                session.add(doc)

            session.commit()
            session.refresh(doc)

            # Insert new chunks
            for idx, (c_model, emb) in enumerate(zip(chunk_models, embeddings)):
                metadata_payload = {
                    "symbol_name": c_model.symbol_name,
                    "symbol_type": c_model.symbol_type,
                    "namespace": c_model.namespace,
                    "imports": c_model.imports,
                    "language": c_model.language,
                    "git_commit_hash": c_model.git_commit_hash,
                    "modified_timestamp": c_model.modified_timestamp.isoformat(),
                    "file_name": c_model.file_name,
                    "file_path": c_model.file_path,
                }

                chunk_id = str(uuid.uuid4())
                db_chunk = DocumentChunk(
                    id=chunk_id,
                    document_id=doc.id,
                    chunk_index=idx,
                    start_line=c_model.start_line,
                    end_line=c_model.end_line,
                    content=c_model.content,
                    embedding_vector_id=chunk_id,
                    metadata_json=json.dumps(metadata_payload),
                    created_at=datetime.utcnow(),
                    updated_at=datetime.utcnow()
                )
                session.add(db_chunk)

                # Update chunk_model with document_id and id for external stores
                c_model.document_id = doc.id
                c_model.id = chunk_id

                indexed_records.append({
                    "id": chunk_id,
                    "document_id": doc.id,
                    "project_id": project_id,
                    "file_path": rel_path_str,
                    "file_name": file_path.name,
                    "chunk_index": idx,
                    "start_line": c_model.start_line,
                    "end_line": c_model.end_line,
                    "content": c_model.content,
                    "symbol_name": c_model.symbol_name,
                    "symbol_type": c_model.symbol_type,
                    "metadata": metadata_payload,
                    "embedding": emb,
                })

            session.commit()

        self._drop_from_stores(project_id, stale_ids, rel_path_str)

        # 4. Upsert into Vector Store (Qdrant) and Keyword Store (FTS5)
        if self.vector_store:
            try:
                self.vector_store.upsert_chunks(
                    project_id=project_id,
                    chunks=chunk_models,
                    embeddings=embeddings
                )
            except Exception as e:
                logger.warning("Vector store upsert failed for %s: %s", rel_path_str, e)

        if self.keyword_store:
            try:
                self.keyword_store.index_chunks(
                    project_id=project_id,
                    chunks=chunk_models
                )
            except Exception as e:
                logger.warning("Keyword store index failed for %s: %s", rel_path_str, e)

        logger.info(
            "Indexed %d chunks for '%s' in project '%s' (DB + Qdrant + FTS5)",
            len(indexed_records),
            file_path.name,
            project_id
        )
        return indexed_records

    def index_project(self, project_id: str) -> dict[str, Any]:
        """
        Scan and index all files in a project workspace.
        Returns indexing summary statistics.
        """
        self.ensure_embedding_generation(project_id)
        files = self.scan_project_paths(project_id)
        indexed_count = 0
        total_chunks = 0

        for f in files:
            chunks = self.index_file(f, project_id=project_id)
            if chunks:
                indexed_count += 1
                total_chunks += len(chunks)

        # Files deleted or moved outside Jarvis since the last pass are still in the index.
        with self._get_session() as session:
            docs = session.exec(select(Document).where(Document.project_id == project_id)).all()
            vanished = [Path(d.file_path) for d in docs if not Path(d.file_path).exists()]
        for path in vanished:
            self.remove_file(path, project_id)

        logger.info(
            "Project %s indexing complete: %d files indexed, %d total chunks created.",
            project_id,
            indexed_count,
            total_chunks
        )
        return {
            "project_id": project_id,
            "scanned_files": len(files),
            "indexed_files": indexed_count,
            "total_chunks": total_chunks,
        }

    def _drop_from_stores(self, project_id: str, chunk_ids: list[str], label: str) -> None:
        if not chunk_ids:
            return
        for name, store in (("Keyword", self.keyword_store), ("Vector", self.vector_store)):
            if store:
                try:
                    store.delete_chunks(project_id, chunk_ids)
                except Exception as e:
                    logger.warning("%s store delete failed for %s: %s", name, label, e)

    def remove_file(self, file_path: Path, project_id: str) -> int:
        """Take a file out of the project's index (DB, FTS5, vectors) -- after it was deleted.
        Returns how many chunks were removed."""
        target = os.path.normcase(os.path.abspath(str(file_path)))
        with self._get_session() as session:
            docs = session.exec(select(Document).where(Document.project_id == project_id)).all()
            doc = next((d for d in docs if os.path.normcase(os.path.abspath(d.file_path)) == target), None)
            if doc is None:
                return 0
            chunks = session.exec(select(DocumentChunk).where(DocumentChunk.document_id == doc.id)).all()
            chunk_ids = [c.id for c in chunks]
            for c in chunks:
                session.delete(c)
            session.delete(doc)
            session.commit()
        self._drop_from_stores(project_id, chunk_ids, str(file_path))
        logger.info("Removed %s from project %s's index (%d chunks)", file_path, project_id, len(chunk_ids))
        return len(chunk_ids)

    def purge_non_text_documents(self) -> int:
        """
        Remove documents (and their chunks, in the DB and both stores) whose file is not a text
        type or whose chunks look binary -- the residue of uploads that were indexed before
        index_file learned to refuse them. Returns how many documents were removed.
        """
        removed = 0
        with SessionLocal() as session:
            docs = session.exec(select(Document)).all()
            for doc in docs:
                path = Path(doc.file_path)
                chunks = session.exec(select(DocumentChunk).where(DocumentChunk.document_id == doc.id)).all()
                sample = "".join((c.content or "")[:512] for c in chunks[:8])
                if path.suffix.lower() in SUPPORTED_EXTENSIONS and not looks_binary(sample):
                    continue
                chunk_ids = [c.id for c in chunks]
                for c in chunks:
                    session.delete(c)
                session.delete(doc)
                session.commit()
                removed += 1
                self._drop_from_stores(doc.project_id, chunk_ids, doc.file_path)
                logger.info("Purged non-text document from the index: %s (%d chunks)", doc.file_path, len(chunk_ids))
        return removed

    def delete_project_index(self, project_id: str) -> None:
        """
        Completely purge vector store and keyword search indexes for a project.
        """
        if self.vector_store:
            try:
                self.vector_store.delete_project_index(project_id)
            except Exception as e:
                logger.warning("Error deleting vector store index for project %s: %s", project_id, e)

        if self.keyword_store:
            try:
                self.keyword_store.delete_project_index(project_id)
            except Exception as e:
                logger.warning("Error deleting keyword store index for project %s: %s", project_id, e)

        try:
            with self._get_session() as session:
                from app.database.models import Document, DocumentChunk
                from sqlmodel import delete as sql_delete
                docs = session.exec(select(Document).where(Document.project_id == project_id)).all()
                for d in docs:
                    session.exec(sql_delete(DocumentChunk).where(DocumentChunk.document_id == d.id))
                    session.delete(d)
                session.commit()
        except Exception as db_e:
            logger.warning("Error deleting document SQL records for project %s: %s", project_id, db_e)


# Backward-compatible alias for WorkspaceIndexer
WorkspaceIndexer = ProjectIndexer

