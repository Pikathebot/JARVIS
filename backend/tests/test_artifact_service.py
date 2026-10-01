import pytest
from sqlmodel import create_engine, SQLModel
from fastapi.testclient import TestClient

from app.main import app
from app.services.artifact_service import ArtifactService
from app.routers.artifacts import get_artifact_service
from app.database.models import Artifact, ArtifactVersion


@pytest.fixture
def isolated_artifact_service(tmp_path):
    """Provides an isolated ArtifactService with an in-memory or tmp_path SQLite database."""
    db_file = tmp_path / "test_artifacts.db"
    engine = create_engine(f"sqlite:///{db_file}")
    SQLModel.metadata.create_all(engine)
    return ArtifactService(db_engine=engine)


def test_create_artifact_initializes_version_1(isolated_artifact_service):
    """Verify create_artifact creates Artifact and v1 ArtifactVersion."""
    artifact = isolated_artifact_service.create_artifact(
        name="hello.py",
        type="code",
        content="print('Hello World')",
        conversation_id="conv_100",
        project_id="proj_1",
        language="python",
        summary="Initial hello world code",
        created_by="agent"
    )

    assert artifact.id is not None
    assert artifact.version == 1
    assert artifact.name == "hello.py"
    assert artifact.language == "python"

    versions = isolated_artifact_service.list_versions(artifact.id)
    assert len(versions) == 1
    assert versions[0].version == 1
    assert versions[0].content == "print('Hello World')"
    assert versions[0].summary == "Initial hello world code"


def test_update_artifact_snapshots_previous_version(isolated_artifact_service):
    """Verify updating content auto-snapshots previous state and increments version number."""
    art = isolated_artifact_service.create_artifact(
        name="notes.md",
        type="markdown",
        content="# Notes v1",
        conversation_id="conv_100",
    )

    # First update
    updated = isolated_artifact_service.update_artifact(
        artifact_id=art.id,
        content="# Notes v2",
        summary="Added chapter 2"
    )
    assert updated.version == 2
    assert updated.content == "# Notes v2"

    versions = isolated_artifact_service.list_versions(art.id)
    assert len(versions) == 2
    assert versions[0].version == 1
    assert versions[0].content == "# Notes v1"
    assert versions[1].version == 2
    assert versions[1].content == "# Notes v2"


def test_list_artifacts_filtering(isolated_artifact_service):
    """Verify listing artifacts supports filtering by conversation, project, and type."""
    isolated_artifact_service.create_artifact(
        name="code1.py", type="code", content="x = 1", conversation_id="conv_A", project_id="proj_1"
    )
    isolated_artifact_service.create_artifact(
        name="doc.md", type="markdown", content="# Doc", conversation_id="conv_A", project_id="proj_2"
    )
    isolated_artifact_service.create_artifact(
        name="code2.py", type="code", content="y = 2", conversation_id="conv_B", project_id="proj_1"
    )

    by_conv = isolated_artifact_service.list_artifacts(conversation_id="conv_A")
    assert len(by_conv) == 2

    by_proj = isolated_artifact_service.list_artifacts(project_id="proj_1")
    assert len(by_proj) == 2

    by_type = isolated_artifact_service.list_artifacts(type="markdown")
    assert len(by_type) == 1
    assert by_type[0].name == "doc.md"


def test_restore_version(isolated_artifact_service):
    """Verify restore_version snapshots active state and rolls back content."""
    art = isolated_artifact_service.create_artifact(
        name="script.py", type="code", content="v1_stable", conversation_id="conv_100"
    )
    isolated_artifact_service.update_artifact(artifact_id=art.id, content="v2_broken")

    # Restore v1
    restored = isolated_artifact_service.restore_version(art.id, version=1, created_by="user")
    assert restored.version == 3
    assert restored.content == "v1_stable"

    versions = isolated_artifact_service.list_versions(art.id)
    assert len(versions) == 3


def test_delete_artifact_cascades_versions(isolated_artifact_service):
    """Verify deleting an artifact removes both the artifact and all version records."""
    art = isolated_artifact_service.create_artifact(
        name="temp.txt", type="document", content="initial"
    )
    isolated_artifact_service.update_artifact(artifact_id=art.id, content="v2")

    assert isolated_artifact_service.delete_artifact(art.id) is True
    assert isolated_artifact_service.get_artifact(art.id) is None

    # Version queries for deleted artifact should raise KeyError
    with pytest.raises(KeyError):
        isolated_artifact_service.list_versions(art.id)


def test_artifacts_rest_api(isolated_artifact_service):
    """Test full REST API lifecycle for artifacts using FastAPI TestClient."""
    app.dependency_overrides[get_artifact_service] = lambda: isolated_artifact_service
    client = TestClient(app)

    try:
        # 1. POST /api/artifacts
        res = client.post(
            "/api/artifacts",
            json={
                "name": "api_test.py",
                "type": "code",
                "content": "val = 42",
                "conversation_id": "c1",
                "language": "python"
            }
        )
        assert res.status_code == 201
        data = res.json()
        art_id = data["id"]
        assert data["version"] == 1
        assert data["name"] == "api_test.py"

        # 2. PATCH /api/artifacts/{id}
        res_patch = client.patch(
            f"/api/artifacts/{art_id}",
            json={"content": "val = 100", "summary": "Updated val"}
        )
        assert res_patch.status_code == 200
        assert res_patch.json()["version"] == 2
        assert res_patch.json()["content"] == "val = 100"

        # 3. GET /api/artifacts/{id}/versions
        res_versions = client.get(f"/api/artifacts/{art_id}/versions")
        assert res_versions.status_code == 200
        assert len(res_versions.json()) == 2

        # 4. POST /api/artifacts/{id}/restore/1
        res_restore = client.post(f"/api/artifacts/{art_id}/restore/1")
        assert res_restore.status_code == 200
        assert res_restore.json()["version"] == 3
        assert res_restore.json()["content"] == "val = 42"

        # 5. DELETE /api/artifacts/{id}
        res_del = client.delete(f"/api/artifacts/{art_id}")
        assert res_del.status_code == 200
        assert res_del.json()["deleted"] is True

    finally:
        app.dependency_overrides.pop(get_artifact_service, None)


# --- patch_artifact: edit part of an artifact without resending all of it (2026-10-01) ---

def test_patch_artifact_replaces_one_block_and_versions_it(isolated_artifact_service, monkeypatch):
    from app.agent.tools import artifacts as art_tools
    monkeypatch.setattr(art_tools, "ArtifactService", lambda: isolated_artifact_service)
    art = isolated_artifact_service.create_artifact(
        name="plan.md", type="document", content="# Plan\nStep one\nStep two\nStep two\n",
        conversation_id="c", project_id="p", created_by="agent",
    )

    out = art_tools.patch_artifact(art.id, "# Plan", "# Launch plan")
    assert out.startswith("Patched 'plan.md'") and "version 2" in out
    assert isolated_artifact_service.get_artifact(art.id).content.startswith("# Launch plan\n")

    assert "appears 2 times" in art_tools.patch_artifact(art.id, "Step two", "Step 2")
    assert "2 replacement(s)" in art_tools.patch_artifact(art.id, "Step two", "Step 2", replace_all=True)

    miss = art_tools.patch_artifact(art.id, "Step tree", "x")
    assert "isn't in 'plan.md'" in miss and "Step one" in miss
    assert isolated_artifact_service.get_artifact(art.id).version == 3


def test_artifacts_are_listed_per_turn_and_scoped_to_their_workspace(isolated_artifact_service, monkeypatch):
    import app.services.artifact_service as svc_mod
    from app.agent import orchestrator
    from app.agent.tools import artifacts as art_tools
    monkeypatch.setattr(svc_mod, "ArtifactService", lambda *a, **k: isolated_artifact_service)
    monkeypatch.setattr(art_tools, "ArtifactService", lambda: isolated_artifact_service)
    mine = isolated_artifact_service.create_artifact(name="Launch plan", type="document", content="x",
                                                     conversation_id="c", project_id="p1", created_by="agent")
    other = isolated_artifact_service.create_artifact(name="Other plan", type="document", content="y",
                                                      conversation_id="c", project_id="p2", created_by="agent")

    listing = orchestrator.workspace_artifact_listing("p1")
    assert f"- Launch plan (document, v1) id: {mine.id}" in listing and "Other plan" not in listing
    assert orchestrator.workspace_artifact_listing("empty") == ""

    assert "not found in this workspace" in art_tools.read_artifact(other.id, project_id="p1")
    assert "not found in this workspace" in art_tools.patch_artifact(other.id, "y", "z", project_id="p1")
    assert "not found in this workspace" in art_tools.update_artifact(other.id, "z", project_id="p1")
    assert "Launch plan" in art_tools.read_artifact(mine.id, project_id="p1")
