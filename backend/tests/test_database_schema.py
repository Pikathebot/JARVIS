import os
import uuid
import pytest
from datetime import datetime
from sqlmodel import Session, select, create_engine
from alembic.config import Config
from alembic import command

from app.database.models import (
    Project,
    Artifact,
    Memory,
    Document,
    DocumentChunk,
    ToolCall,
    AgentRun,
    Session as DBSession,
    Message as DBMessage,
)
from app.database.session import get_db


def test_sqlmodel_instantiations():
    """Verify that all SQLModel models can be instantiated with defaults."""
    project = Project(name="Test Project", description="Test Description")
    assert project.id is not None
    assert project.name == "Test Project"
    assert project.created_at is not None
    assert project.updated_at is not None

    artifact = Artifact(
        project_id=project.id,
        name="test.py",
        type="code",
        content="print('hello')"
    )
    assert artifact.id is not None
    assert artifact.version == 1
    assert artifact.created_at is not None
    assert artifact.updated_at is not None

    memory = Memory(
        project_id=project.id,
        category="preference",
        content="Prefers dark mode"
    )
    assert memory.id is not None
    assert memory.confidence == 1.0
    assert memory.pinned is False
    assert memory.created_at is not None
    assert memory.updated_at is not None

    doc = Document(
        project_id=project.id,
        file_path="/path/to/test.py",
        file_name="test.py"
    )
    assert doc.id is not None
    assert doc.created_at is not None
    assert doc.updated_at is not None

    chunk = DocumentChunk(
        document_id=doc.id,
        chunk_index=0,
        content="print('hello')"
    )
    assert chunk.id is not None
    assert chunk.created_at is not None
    assert chunk.updated_at is not None

    tool_call = ToolCall(
        session_id="test_session",
        call_id="call_123",
        tool_name="read_file"
    )
    assert tool_call.id is not None
    assert tool_call.status == "pending"
    assert tool_call.created_at is not None
    assert tool_call.updated_at is not None

    agent_run = AgentRun(
        session_id="test_session",
        project_id=project.id,
        status="running"
    )
    assert agent_run.id is not None
    assert agent_run.tokens_in == 0
    assert agent_run.tokens_out == 0
    assert agent_run.created_at is not None
    assert agent_run.updated_at is not None


def test_alembic_migration_on_fresh_db(tmp_path):
    """Verify that Alembic migrations run cleanly on a fresh temporary database."""
    db_file = tmp_path / "fresh_test.db"
    db_url = f"sqlite:///{db_file.resolve().as_posix()}"

    backend_dir = os.path.dirname(os.path.dirname(__file__))
    alembic_ini_path = os.path.join(backend_dir, "alembic.ini")

    alembic_cfg = Config(alembic_ini_path)
    alembic_cfg.set_main_option("script_location", os.path.join(backend_dir, "alembic"))
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)
    # As the app does (main.py): without this env.py runs fileConfig(), which disables every
    # jarvis.* logger for the rest of the pytest process and breaks caplog-based tests after it.
    alembic_cfg.attributes["configure_logging_skipped"] = True

    # Run upgrade
    command.upgrade(alembic_cfg, "head")

    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    with Session(engine) as session:
        # Verify seeded Default Workspace project exists
        projects = session.exec(select(Project)).all()
        assert len(projects) == 1
        default_proj = projects[0]
        assert default_proj.id == "default-workspace"
        assert default_proj.name == "Default Workspace"

        # Verify creating records across models
        test_session = DBSession(session_id="sess_migration_test", title="Migration Test")
        session.add(test_session)
        session.commit()

        test_msg = DBMessage(session_id="sess_migration_test", role="user", content="Hello")
        session.add(test_msg)
        session.commit()

        artifact = Artifact(
            project_id=default_proj.id,
            session_id=test_session.session_id,
            name="README.md",
            type="markdown",
            content="# Hello World"
        )
        session.add(artifact)
        session.commit()
        session.refresh(artifact)
        assert artifact.id is not None

        tool_call = ToolCall(
            session_id=test_session.session_id,
            call_id="call_abc",
            tool_name="list_directory",
            status="success"
        )
        session.add(tool_call)
        session.commit()
        session.refresh(tool_call)
        assert tool_call.id is not None


def test_get_db_dependency():
    """Verify get_db dependency yields a working Session."""
    db_gen = get_db()
    session = next(db_gen)
    assert isinstance(session, Session)
    # Check query execution
    projects = session.exec(select(Project)).all()
    assert len(projects) >= 1
    try:
        next(db_gen)
    except StopIteration:
        pass
