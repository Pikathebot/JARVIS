import io
import os
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, create_engine, Session

from app.main import app
from app.config import settings
from app.database import get_session
from app.database.models import Project


@pytest.fixture
def upload_test_env(tmp_path, monkeypatch):
    """Isolated database and workspace directory for file upload testing."""
    test_db = tmp_path / "test_uploads.db"
    db_url = f"sqlite:///{test_db}"
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)

    test_workspace = tmp_path / "workspace"
    test_workspace.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "workspace_path", str(test_workspace))
    monkeypatch.setattr(settings, "max_upload_size_mb", 2)  # 2MB for testing

    monkeypatch.setattr("app.database.SessionLocal", lambda: Session(engine))
    monkeypatch.setattr("app.database.session.SessionLocal", lambda: Session(engine))

    def override_get_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    client = TestClient(app)
    yield client, test_workspace, engine

    app.dependency_overrides.clear()



def test_upload_allowed_file_success(upload_test_env):
    client, test_workspace, *rest = upload_test_env

    # 1. Create a project first
    proj_res = client.post("/api/projects", json={"name": "Upload Target"})
    assert proj_res.status_code == 201
    project_id = proj_res.json()["id"]

    file_content = b"print('Hello Secure Upload World')\n"
    files = {"file": ("script.py", io.BytesIO(file_content), "text/x-python")}
    data = {"project_id": project_id, "session_id": "test_sess_1"}

    res = client.post("/api/upload", files=files, data=data)
    assert res.status_code == 201
    res_data = res.json()

    assert res_data["filename"] == "script.py"
    assert res_data["project_id"] == project_id
    assert res_data["session_id"] == "test_sess_1"
    assert res_data["size_bytes"] == len(file_content)

    # Check file exists on disk inside project's files/ folder
    saved_path = Path(res_data["path"])
    assert saved_path.exists()
    assert saved_path.read_bytes() == file_content
    assert str(test_workspace) in str(saved_path)

    # Test list project files endpoint (Section 19 / Amendment 4)
    files_list_res = client.get(f"/api/projects/{project_id}/files")
    assert files_list_res.status_code == 200
    p_files = files_list_res.json()
    assert len(p_files) == 1
    assert p_files[0]["name"] == "script.py"


def test_upload_extension_whitelist_rejection(upload_test_env):
    client, *_ = upload_test_env


    # Try uploading a disallowed executable (.exe)
    bad_file = {"file": ("malware.exe", io.BytesIO(b"dangerous_bytes"), "application/octet-stream")}
    res = client.post("/api/upload", files=bad_file)
    assert res.status_code == 400
    assert "not permitted" in res.json()["detail"]


def test_upload_size_limit_rejection(upload_test_env):
    client, *_ = upload_test_env


    # Over 2MB limit
    oversized = b"0" * (3 * 1024 * 1024)
    file_payload = {"file": ("large.txt", io.BytesIO(oversized), "text/plain")}

    res = client.post("/api/upload", files=file_payload)
    assert res.status_code == 413
    assert "exceeds maximum limit" in res.json()["detail"]


def test_upload_path_traversal_sanitization(upload_test_env):
    client, test_workspace, *rest = upload_test_env

    # Malicious filename attempting traversal
    traversal_file = {"file": ("../../evil_traversal.py", io.BytesIO(b"# safe"), "text/x-python")}
    res = client.post("/api/upload", files=traversal_file)
    assert res.status_code == 201

    data = res.json()
    # Path traversal characters should be stripped
    assert ".." not in data["filename"]
    saved_path = Path(data["path"])
    assert saved_path.exists()
    assert str(test_workspace) in str(saved_path)


def test_attachment_read_by_orchestrator_and_read_file_tool(upload_test_env):
    from app.agent.tools.read_file import read_file
    from app.agent.orchestrator import AgentOrchestrator
    from app.memory.store import MemoryStore
    from app.memory.compactor import ContextCompactor

    client, test_workspace, *rest = upload_test_env

    # 1. Upload a python file attachment
    code_bytes = b"def calculate_total(prices):\n    return sum(prices)\n"
    res = client.post("/api/upload", files={"file": ("calc_utils.py", io.BytesIO(code_bytes), "text/x-python")}, data={"session_id": "sess_calc_1"})
    assert res.status_code == 201
    upload_data = res.json()

    # 2. Test read_file tool can resolve by name directly from workspace
    content = read_file("calc_utils.py")
    assert "calculate_total" in content

    # 3. Test AgentOrchestrator._build_attachment_prompt injects file contents
    store = MemoryStore()
    compactor = ContextCompactor()
    orch = AgentOrchestrator(memory_store=store, compactor=compactor)
    prompt_context = orch._build_attachment_prompt(session_id="sess_calc_1")

    assert "USER ATTACHED FILES" in prompt_context
    assert "calc_utils.py" in prompt_context
    assert "calculate_total" in prompt_context



def test_upload_into_a_session_that_has_no_messages_yet(tmp_path, monkeypatch):
    """The client uploads before the first message, so the session row may not exist yet."""
    from fastapi.testclient import TestClient
    from app.config import settings
    from app.main import app

    import uuid
    session_id = f"brand-new-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(settings, "workspace_path", str(tmp_path))
    png = tmp_path / "shot.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    with TestClient(app) as client:
        r = client.post("/api/upload", files={"file": ("shot.png", png.read_bytes(), "image/png")},
                        data={"session_id": session_id})
        assert r.status_code == 201, r.text
        assert r.json()["session_id"] == session_id
        listed = client.get("/api/attachments", params={"session_id": session_id}).json()
        items = listed if isinstance(listed, list) else listed.get("attachments", listed)
        assert [a["filename"] for a in items] == ["shot.png"]
