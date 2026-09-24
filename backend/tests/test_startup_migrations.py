"""The startup migration shortcut: Alembic is only imported when the database is behind."""
import sqlite3
from pathlib import Path

import pytest

import app.main as main
from app.config import settings

BACKEND_DIR = Path(main.__file__).resolve().parent.parent


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    path = tmp_path / "jarvis_test.db"
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{path.as_posix()}")
    return path


def _stamp(path: Path, revision: str) -> None:
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)")
        db.execute("DELETE FROM alembic_version")
        db.execute("INSERT INTO alembic_version VALUES (?)", (revision,))


def test_a_migrated_database_is_at_head(temp_db):
    main.run_db_migrations()  # a fresh file: the full Alembic upgrade
    assert main._database_at_head(BACKEND_DIR) is True


def test_an_older_stamp_is_not_at_head(temp_db):
    main.run_db_migrations()
    with sqlite3.connect(temp_db) as db:
        head = db.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    _stamp(temp_db, "001")
    assert head != "001"
    assert main._database_at_head(BACKEND_DIR) is False


def test_missing_database_or_version_table_is_not_at_head(temp_db):
    assert main._database_at_head(BACKEND_DIR) is False  # no file yet
    sqlite3.connect(temp_db).close()
    assert main._database_at_head(BACKEND_DIR) is False  # file, no alembic_version


def test_a_database_behind_head_still_gets_migrated(temp_db):
    main.run_db_migrations()
    with sqlite3.connect(temp_db) as db:
        db.execute("ALTER TABLE messages DROP COLUMN reasoning_content")
    _stamp(temp_db, "005")  # as if 006 had never run
    main.run_db_migrations()
    with sqlite3.connect(temp_db) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
        stamped = db.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    assert "reasoning_content" in columns
    assert stamped == "006"
