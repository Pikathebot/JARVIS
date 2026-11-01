import os

import pytest

from app import atomic_write
from app.atomic_write import write_text_atomic


def test_writes_and_creates_the_folder(tmp_path):
    target = tmp_path / "a" / "state.json"
    write_text_atomic(target, '{"x": 1}')
    assert target.read_text(encoding="utf-8") == '{"x": 1}'
    assert [p.name for p in target.parent.iterdir()] == ["state.json"]


def test_a_crash_mid_write_keeps_the_old_file_and_leaves_no_temp(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    target.write_text("old", encoding="utf-8")

    def boom(*_):
        raise OSError("disk went away")

    monkeypatch.setattr(atomic_write.os, "replace", boom)
    with pytest.raises(OSError):
        write_text_atomic(target, "new")
    assert target.read_text(encoding="utf-8") == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_replaces_existing_contents(tmp_path):
    target = tmp_path / "state.json"
    target.write_text("old", encoding="utf-8")
    write_text_atomic(target, "new")
    assert target.read_text(encoding="utf-8") == "new"
