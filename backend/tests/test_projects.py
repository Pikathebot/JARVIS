import os
import shutil
import tempfile
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, create_engine, Session

from app.main import app
from app.config import settings
from app.database import get_session
from app.database.models import Project, Session as DBSession
from app.memory.store import MemoryStore


@pytest.fixture
def project_test_env(tmp_path, monkeypatch):
    """Isolated database and workspace directory for projects testing."""
    test_db = tmp_path / "test_projects.db"
    db_url = f"sqlite:///{test_db}"
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)

    test_workspace = tmp_path / "workspace"
    test_workspace.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "workspace_path", str(test_workspace))

    def override_get_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session

    # Also point memory_store to this test db
    test_memory_store = MemoryStore(session_factory=lambda: Session(engine))
    monkeypatch.setattr("app.main.memory_store", test_memory_store)

    client = TestClient(app)
    yield client, test_workspace, test_memory_store

    app.dependency_overrides.clear()


def test_create_project_initializes_filesystem_and_subdirectories(project_test_env):
    client, test_workspace, _ = project_test_env

    payload = {
        "name": "Apollo Project",
        "description": "Lunar navigation systems",
        "instructions": "Focus on fault-tolerant algorithms.",
        "local_folders": ["/home/user/src", "/home/user/docs"]
    }

    res = client.post("/api/projects", json=payload)
    assert res.status_code == 201
    data = res.json()

    assert data["name"] == "Apollo Project"
    assert data["description"] == "Lunar navigation systems"
    assert data["instructions"] == "Focus on fault-tolerant algorithms."
    assert data["local_folders"] == ["/home/user/src", "/home/user/docs"]
    assert data["is_active"] is True  # First project becomes active

    project_id = data["id"]
    project_dir = Path(test_workspace) / "projects" / project_id
    assert project_dir.exists()

    # Section 7 filesystem layout verification
    expected_subdirs = ["files", "knowledge", "artifacts", "memory", "indexes"]
    for sub in expected_subdirs:
        sub_path = project_dir / sub
        assert sub_path.exists(), f"Subdirectory '{sub}' must be created"
        assert sub_path.is_dir()


def test_list_and_get_projects(project_test_env):
    client, _, _ = project_test_env

    # 1. Initially empty list
    res = client.get("/api/projects")
    assert res.status_code == 200
    assert res.json() == []

    # 2. Create 2 projects
    p1 = client.post("/api/projects", json={"name": "Alpha"}).json()
    p2 = client.post("/api/projects", json={"name": "Beta"}).json()

    res_list = client.get("/api/projects").json()
    assert len(res_list) == 2
    names = [p["name"] for p in res_list]
    assert "Alpha" in names
    assert "Beta" in names

    # 3. Get single project
    res_get = client.get(f"/api/projects/{p1['id']}")
    assert res_get.status_code == 200
    assert res_get.json()["id"] == p1["id"]

    # 4. Get non-existent
    res_404 = client.get("/api/projects/non-existent-uuid")
    assert res_404.status_code == 404


def test_update_and_activate_project(project_test_env):
    client, _, _ = project_test_env

    p1 = client.post("/api/projects", json={"name": "Project One"}).json()
    p2 = client.post("/api/projects", json={"name": "Project Two"}).json()

    # Initial active check
    current = client.get("/api/projects/active/current").json()
    assert current is not None
    assert current["id"] == p1["id"]

    # Activate Project Two
    act_res = client.post(f"/api/projects/{p2['id']}/activate")
    assert act_res.status_code == 200
    assert act_res.json()["is_active"] is True

    # Check active is switched
    current_after = client.get("/api/projects/active/current").json()
    assert current_after["id"] == p2["id"]

    # Update project
    up_res = client.put(
        f"/api/projects/{p1['id']}",
        json={
            "name": "Project One Renamed",
            "local_folders": ["/new/path/alpha"],
            "instructions": "Updated system instructions"
        }
    )
    assert up_res.status_code == 200
    updated_data = up_res.json()
    assert updated_data["name"] == "Project One Renamed"
    assert updated_data["local_folders"] == ["/new/path/alpha"]
    assert updated_data["instructions"] == "Updated system instructions"


def test_delete_project_cleans_workspace_directory(project_test_env):
    client, test_workspace, _ = project_test_env

    p = client.post("/api/projects", json={"name": "Temporary Project"}).json()
    project_id = p["id"]
    project_dir = Path(test_workspace) / "projects" / project_id
    assert project_dir.exists()

    # Write a dummy file in the project's files directory
    dummy_file = project_dir / "files" / "sample.txt"
    dummy_file.write_text("sample project file content", encoding="utf-8")
    assert dummy_file.exists()

    # Delete project
    del_res = client.delete(f"/api/projects/{project_id}?clean_files=true")
    assert del_res.status_code == 200
    assert del_res.json()["deleted"] is True

    # Verify directory was deleted from disk
    assert not project_dir.exists()

    # Verify record removed from DB
    get_res = client.get(f"/api/projects/{project_id}")
    assert get_res.status_code == 404


def test_new_chat_is_stored_at_once_and_an_empty_one_is_reused(project_test_env):
    client, _, store = project_test_env
    p1 = client.post("/api/projects", json={"name": "Project 1"}).json()
    p2 = client.post("/api/projects", json={"name": "Project 2"}).json()

    # Pressing New chat records the session before any message is sent.
    first = client.post("/sessions", json={"session_id": "s_new_1", "chat_mode": "WORKSPACE", "project_id": p1["id"]})
    assert first.status_code == 200 and first.json()["session_id"] == "s_new_1"
    assert [s["session_id"] for s in client.get(f"/sessions?project_id={p1['id']}").json()] == ["s_new_1"]

    # Pressing it again (or reopening the app) hands back the empty one instead of adding another.
    again = client.post("/sessions", json={"session_id": "s_new_2", "chat_mode": "WORKSPACE", "project_id": p1["id"]})
    assert again.json()["session_id"] == "s_new_1"
    assert len(client.get(f"/sessions?project_id={p1['id']}").json()) == 1

    # Another project has its own; once a chat has a message, the next New chat is a fresh row.
    assert client.post("/sessions", json={"session_id": "s_p2", "chat_mode": "WORKSPACE", "project_id": p2["id"]}).json()["session_id"] == "s_p2"
    store.append_message("s_new_1", role="user", content="hello")
    fresh = client.post("/sessions", json={"session_id": "s_new_3", "chat_mode": "WORKSPACE", "project_id": p1["id"]})
    assert fresh.json()["session_id"] == "s_new_3"

    # Freeform chats are their own space, and ephemeral ones are never stored.
    assert client.post("/sessions", json={"session_id": "s_free", "chat_mode": "FREEFORM"}).json()["session_id"] == "s_free"
    assert client.post("/sessions", json={"session_id": "eph-abc", "chat_mode": "WORKSPACE"}).status_code == 400


def test_session_filtering_by_project(project_test_env):
    client, _, store = project_test_env

    p1 = client.post("/api/projects", json={"name": "Project 1"}).json()
    p2 = client.post("/api/projects", json={"name": "Project 2"}).json()

    # Create sessions assigned to p1 and p2
    store.get_or_create_session("sess_p1_a", project_id=p1["id"])
    store.get_or_create_session("sess_p1_b", project_id=p1["id"])
    store.get_or_create_session("sess_p2_a", project_id=p2["id"])
    store.get_or_create_session("sess_unassigned")

    # 1. Fetch all sessions (no filter)
    all_sess = client.get("/sessions").json()
    assert len(all_sess) == 4

    # 2. Fetch sessions filtered by Project 1
    p1_sess = client.get(f"/sessions?project_id={p1['id']}").json()
    assert len(p1_sess) == 2
    session_ids = [s["session_id"] for s in p1_sess]
    assert "sess_p1_a" in session_ids
    assert "sess_p1_b" in session_ids
    assert "sess_p2_a" not in session_ids

    # 3. Fetch sessions filtered by Project 2
    p2_sess = client.get(f"/sessions?project_id={p2['id']}").json()
    assert len(p2_sess) == 1
    assert p2_sess[0]["session_id"] == "sess_p2_a"
