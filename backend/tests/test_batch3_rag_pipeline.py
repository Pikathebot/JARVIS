import io
import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, create_engine, Session, select

from app.main import app
from app.config import settings
from app.database import get_session
from app.database.models import Project, Document, DocumentChunk
from app.agent.tools.write_file import write_file
from app.rag.indexer import WorkspaceIndexer


@pytest.fixture
def batch3_env(tmp_path, monkeypatch):
    test_db = tmp_path / "test_batch3.db"
    db_url = f"sqlite:///{test_db}"
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)

    test_workspace = tmp_path / "custom_ws"
    test_workspace.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "workspace_path", str(test_workspace))

    monkeypatch.setattr("app.database.SessionLocal", lambda: Session(engine))
    monkeypatch.setattr("app.database.session.SessionLocal", lambda: Session(engine))

    def override_get_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    client = TestClient(app)

    yield client, test_workspace, engine

    app.dependency_overrides.clear()


def test_custom_workspace_scanned_by_indexer(batch3_env):
    client, test_workspace, engine = batch3_env

    secret_file = test_workspace / "secret.txt"
    secret_file.write_text("The secret fruit is pineapple.", encoding="utf-8")

    code_file = test_workspace / "main.py"
    code_file.write_text("def run():\n    return 'JARVIS'\n", encoding="utf-8")

    git_folder = test_workspace / ".git"
    git_folder.mkdir(parents=True, exist_ok=True)
    (git_folder / "config.txt").write_text("git internal", encoding="utf-8")

    with Session(engine) as session:
        proj = Project(
            id="proj-b3-custom",
            name="QA Test Project",
            workspace_path=str(test_workspace),
            is_active=True
        )
        session.add(proj)
        session.commit()

    indexer = WorkspaceIndexer()
    scanned = indexer.scan_project_paths("proj-b3-custom")
    scanned_names = [p.name for p in scanned]

    assert "secret.txt" in scanned_names
    assert "main.py" in scanned_names
    assert "config.txt" not in scanned_names


def test_write_file_triggers_automatic_indexing(batch3_env):
    client, test_workspace, engine = batch3_env

    with Session(engine) as session:
        proj = Project(
            id="proj-b3-write",
            name="Write Trigger Project",
            workspace_path=str(test_workspace),
            is_active=True
        )
        session.add(proj)
        session.commit()

    res = write_file(
        file_path="notes.md",
        content="# Important Notes\nThis is auto-indexed.\n",
        workspace_path=str(test_workspace),
        project_id="proj-b3-write"
    )
    assert "Successfully wrote" in res

    with Session(engine) as session:
        doc = session.exec(
            select(Document).where(Document.project_id == "proj-b3-write")
        ).first()
        assert doc is not None
        assert doc.file_name == "notes.md"

        chunks = session.exec(
            select(DocumentChunk).where(DocumentChunk.document_id == doc.id)
        ).all()
        assert len(chunks) > 0


def test_project_index_endpoint(batch3_env):
    client, test_workspace, engine = batch3_env

    (test_workspace / "api_doc.md").write_text("API documentation for testing.", encoding="utf-8")

    with Session(engine) as session:
        proj = Project(
            id="proj-b3-endpoint",
            name="Endpoint Test",
            workspace_path=str(test_workspace),
            is_active=True
        )
        session.add(proj)
        session.commit()

    res = client.post("/api/projects/proj-b3-endpoint/index")
    assert res.status_code == 200
    data = res.json()
    assert data["project_id"] == "proj-b3-endpoint"
    assert data["scanned_files"] >= 1
    assert data["indexed_files"] >= 1
    assert data["total_chunks"] >= 1


def test_upload_triggers_automatic_indexing(batch3_env):
    client, test_workspace, engine = batch3_env

    with Session(engine) as session:
        proj = Project(
            id="proj-b3-upload",
            name="Upload Index Project",
            workspace_path=str(test_workspace),
            is_active=True
        )
        session.add(proj)
        session.commit()

    file_content = b"Special instructions for JARVIS uploaded via API.\n"
    files = {"file": ("instructions.txt", io.BytesIO(file_content), "text/plain")}
    data = {"project_id": "proj-b3-upload"}

    res = client.post("/api/upload", files=files, data=data)
    assert res.status_code == 201

    with Session(engine) as session:
        doc = session.exec(
            select(Document).where(Document.project_id == "proj-b3-upload")
        ).first()
        assert doc is not None
        assert doc.file_name == "instructions.txt"

        chunks = session.exec(
            select(DocumentChunk).where(DocumentChunk.document_id == doc.id)
        ).all()
        assert len(chunks) > 0


@pytest.mark.asyncio
async def test_orchestrator_injects_rag_context_from_active_project(batch3_env, monkeypatch):
    client, test_workspace, engine = batch3_env

    # 1. Create a file in workspace and index it
    secret_path = test_workspace / "secret.txt"
    secret_path.write_text("The secret code is 998877.", encoding="utf-8")

    with Session(engine) as session:
        proj = Project(
            id="proj-b3-orchestrator",
            name="Active Orchestrator Project",
            workspace_path=str(test_workspace),
            is_active=True
        )
        session.add(proj)
        session.commit()

    indexer = WorkspaceIndexer()
    indexer.index_project("proj-b3-orchestrator")

    # 2. Build orchestrator sharing the same vector_store to prevent Qdrant locks
    from app.agent.orchestrator import AgentOrchestrator, OrchestratorResult
    from app.memory.store import MemoryStore
    from app.rag.retriever import HybridRetriever

    retriever = HybridRetriever(
        vector_store=indexer.vector_store,
        keyword_store=indexer.keyword_store
    )
    mem_store = MemoryStore(session_factory=lambda: Session(engine))
    orch = AgentOrchestrator(memory_store=mem_store, retriever=retriever)

    captured_system_prompt = None
    captured_messages = None

    async def mock_run_provider_loop(*args, **kwargs):
        nonlocal captured_system_prompt, captured_messages
        captured_system_prompt = kwargs.get("system_prompt")
        captured_messages = kwargs.get("conversation_messages")
        return OrchestratorResult(
            response="Found secret",
            model="test",
            provider="test"
        )

    orch._run_provider_loop = mock_run_provider_loop

    # 3. Call run in WORKSPACE mode WITHOUT passing project_id explicitly
    result = await orch.run(
        user_message="What is the secret code?",
        chat_mode="WORKSPACE",
        project_id=None
    )

    assert result is not None
    assert captured_system_prompt is not None
    # Retrieved chunks ride on the current user turn, not the head system message: the system
    # prompt is the cached prefix and must not change from turn to turn.
    assert "998877" not in captured_system_prompt
    assert captured_messages and captured_messages[-1]["role"] == "user"
    assert "998877" in captured_messages[-1]["content"]

