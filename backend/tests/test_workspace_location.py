"""A workspace can be a real folder the user picked (PLAN 4.10): it is used as is, files go
there, Jarvis's bookkeeping stays in its own project dir, and deleting the workspace never
touches the user's folder."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlmodel import Session

from app.database.models import Project
from app.rag.indexer import ProjectIndexer
from app.routers.artifacts import get_target_files_dir
from app.workspaces import attachments_dir, is_external, jarvis_project_dir

from tests.test_projects import project_test_env  # noqa: F401  (fixture)


@pytest.fixture
def user_folder(tmp_path):
    folder = tmp_path / "MyProject"
    folder.mkdir()
    (folder / "notes.md").write_text("# notes", encoding="utf-8")
    return folder


def test_a_picked_folder_becomes_the_workspace(project_test_env, user_folder):
    client, test_workspace, _ = project_test_env
    resp = client.post("/api/projects", json={"name": "Mine", "workspace_path": str(user_folder)})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert Path(body["workspace_path"]) == user_folder.resolve()
    # The user's folder is untouched; Jarvis's own dir exists for its bookkeeping.
    assert sorted(p.name for p in user_folder.iterdir()) == ["notes.md"]
    assert (Path(test_workspace) / "projects" / body["id"] / "indexes").is_dir()


def test_no_folder_means_a_folder_of_jarvis_own(project_test_env):
    client, test_workspace, _ = project_test_env
    body = client.post("/api/projects", json={"name": "Plain"}).json()
    assert Path(body["workspace_path"]) == (Path(test_workspace) / "projects" / body["id"]).resolve()


@pytest.mark.parametrize("bad", ["relative/path", "C:/definitely/not/here/xyz"])
def test_a_location_that_is_not_an_existing_absolute_folder_is_refused(project_test_env, bad):
    client, _, _ = project_test_env
    resp = client.post("/api/projects", json={"name": "Bad", "workspace_path": bad})
    assert resp.status_code == 400


def test_deleting_the_workspace_keeps_the_users_folder(project_test_env, user_folder):
    client, test_workspace, _ = project_test_env
    pid = client.post("/api/projects", json={"name": "Mine", "workspace_path": str(user_folder)}).json()["id"]
    assert client.delete(f"/api/projects/{pid}").status_code == 200
    assert (user_folder / "notes.md").exists()
    assert not (Path(test_workspace) / "projects" / pid).exists()


def test_local_folders_no_longer_move_the_workspace(project_test_env, tmp_path):
    client, _, _ = project_test_env
    body = client.post("/api/projects", json={"name": "Plain"}).json()
    extra = tmp_path / "extra"
    extra.mkdir()
    updated = client.put(f"/api/projects/{body['id']}", json={"local_folders": [str(extra)]}).json()
    assert updated["workspace_path"] == body["workspace_path"]
    assert updated["local_folders"] == [str(extra)]


def test_uploads_to_a_picked_folder_go_to_its_hidden_jarvis_folder(project_test_env, user_folder, monkeypatch):
    client, _, _ = project_test_env
    pid = client.post("/api/projects", json={"name": "Mine", "workspace_path": str(user_folder)}).json()["id"]
    from app.database import get_session
    from app.main import app

    db: Session = next(app.dependency_overrides[get_session]())
    target = get_target_files_dir(pid, db)
    assert target == user_folder.resolve() / ".jarvis" / "attachments"
    assert not (user_folder / "files").exists()


def test_internal_workspace_keeps_its_files_folder(project_test_env):
    _, test_workspace, _ = project_test_env
    internal = str(jarvis_project_dir("abc"))
    assert not is_external(internal, "abc")
    assert attachments_dir(internal, "abc") == jarvis_project_dir("abc") / "files"


def test_indexer_scans_the_picked_folder_and_its_attachments(project_test_env, user_folder):
    client, _, _ = project_test_env
    pid = client.post("/api/projects", json={"name": "Mine", "workspace_path": str(user_folder)}).json()["id"]
    attachments = user_folder / ".jarvis" / "attachments"
    attachments.mkdir(parents=True)
    (attachments / "spec.md").write_text("# spec", encoding="utf-8")
    (user_folder / ".git").mkdir()
    (user_folder / ".git" / "config.md").write_text("x", encoding="utf-8")

    from app.database import get_session
    from app.main import app

    factory = lambda: next(app.dependency_overrides[get_session]())  # noqa: E731
    indexer = ProjectIndexer(session_factory=factory, embedding_service=MagicMock(),
                             vector_store=MagicMock(), keyword_store=MagicMock())
    names = sorted(p.name for p in indexer.scan_project_paths(pid))
    assert names == ["notes.md", "spec.md"]
