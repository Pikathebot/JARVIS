import sqlite3
import zipfile
from pathlib import Path

import pytest

from app.backup import BackupError, BackupLocations, create_backup, restore_backup


def _locations(root: Path) -> BackupLocations:
    return BackupLocations(
        db_path=root / "backend" / "data" / "jarvis_memory.db",
        data_dir=root / "data",
        workspace_dir=root / "backend" / "workspace",
        notes_dir=root / "notes",
    )


def _populate(loc: BackupLocations) -> None:
    loc.db_path.parent.mkdir(parents=True)
    db = sqlite3.connect(loc.db_path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE chat (id INTEGER PRIMARY KEY, text TEXT)")
    db.execute("INSERT INTO chat (text) VALUES ('hello from the wal')")
    db.commit()  # left open: the row may still sit in the -wal file, which a plain file copy would miss
    (loc.data_dir / "history-archive").mkdir(parents=True)
    (loc.data_dir / "persona.json").write_text('{"active_id": "jarvis"}', encoding="utf-8")
    (loc.data_dir / "history-archive" / "old.jsonl").write_text("{}\n", encoding="utf-8")
    (loc.data_dir / "google_token.bin").write_bytes(b"secret")
    (loc.data_dir / "remote_vapid.pem").write_bytes(b"key")
    (loc.data_dir / "webview_cache").mkdir()
    (loc.data_dir / "webview_cache" / "blob").write_bytes(b"cache")
    project = loc.workspace_dir / "projects" / "p1"
    (project / "artifacts").mkdir(parents=True)
    (project / "artifacts" / "plan.md").write_text("# plan", encoding="utf-8")
    (project / "indexes" / "qdrant").mkdir(parents=True)
    (project / "indexes" / "qdrant" / "vec.bin").write_bytes(b"vectors")
    (loc.workspace_dir / "files").mkdir()
    (loc.workspace_dir / "files" / "doc.txt").write_text("loose", encoding="utf-8")
    loc.notes_dir.mkdir()
    (loc.notes_dir / "tasks.md").write_text("- milk", encoding="utf-8")
    db.close()


def test_backup_then_restore_onto_a_clean_checkout(tmp_path):
    src = _locations(tmp_path / "live")
    _populate(src)
    report = create_backup(src, tmp_path / "out" / "b.zip")
    assert report.path.is_file() and report.files >= 6

    clean = _locations(tmp_path / "clean")
    restored = restore_backup(report.path, clean)

    assert restored == report.files
    assert sqlite3.connect(clean.db_path).execute("SELECT text FROM chat").fetchone() == ("hello from the wal",)
    assert (clean.data_dir / "persona.json").read_text(encoding="utf-8") == '{"active_id": "jarvis"}'
    assert (clean.data_dir / "history-archive" / "old.jsonl").is_file()
    assert (clean.workspace_dir / "projects" / "p1" / "artifacts" / "plan.md").read_text(encoding="utf-8") == "# plan"
    assert (clean.workspace_dir / "files" / "doc.txt").is_file()
    assert (clean.notes_dir / "tasks.md").read_text(encoding="utf-8") == "- milk"


def test_secrets_caches_and_indexes_are_left_out_by_default(tmp_path):
    src = _locations(tmp_path / "live")
    _populate(src)
    report = create_backup(src, tmp_path / "b.zip")
    names = zipfile.ZipFile(report.path).namelist()
    assert not any("google_token" in n or "vapid" in n for n in names)
    assert not any("webview_cache" in n or "/indexes/" in n for n in names)

    with_secrets = create_backup(src, tmp_path / "s.zip", include_secrets=True)
    assert "data/google_token.bin" in zipfile.ZipFile(with_secrets.path).namelist()


def test_restore_refuses_to_overwrite_unless_told(tmp_path):
    src = _locations(tmp_path / "live")
    _populate(src)
    archive = create_backup(src, tmp_path / "b.zip").path
    with pytest.raises(BackupError, match="already exist"):
        restore_backup(archive, src)
    (src.data_dir / "persona.json").write_text("changed", encoding="utf-8")
    restore_backup(archive, src, overwrite=True)
    assert (src.data_dir / "persona.json").read_text(encoding="utf-8") == '{"active_id": "jarvis"}'


def test_restore_refuses_entries_that_escape_the_target(tmp_path):
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("manifest.json", '{"format": 1}')
        zf.writestr("data/../../escaped.txt", "x")
    with pytest.raises(BackupError, match="escapes"):
        restore_backup(evil, _locations(tmp_path / "clean"))
    assert not (tmp_path / "escaped.txt").exists()


def test_restore_rejects_a_zip_that_is_not_a_backup(tmp_path):
    other = tmp_path / "other.zip"
    with zipfile.ZipFile(other, "w") as zf:
        zf.writestr("readme.txt", "hi")
    with pytest.raises(BackupError, match="manifest"):
        restore_backup(other, _locations(tmp_path / "clean"))


def test_backup_endpoint_writes_a_zip_where_it_says(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setenv("JARVIS_BACKUP_DIR", str(tmp_path / "out"))
    reply = TestClient(app).post("/api/backup", json={})
    assert reply.status_code == 200
    body = reply.json()
    assert Path(body["path"]).parent == tmp_path / "out"
    assert "manifest.json" in zipfile.ZipFile(body["path"]).namelist()
