"""find_files / grep_in_files / list_directory / read_file after the tool review (2026-10-01):
skip only clutter, sniff binaries, cut long lines, show context, say which file was read."""
import os
import time

from app.agent.tools.file_search import find_files, grep_in_files
from app.agent.tools.list_directory import list_directory
from app.agent.tools.read_file import read_file


def _ws(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "chart.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00binary")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "notes.txt").write_text("alpha\nneedle here\nomega\n", encoding="utf-8")
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "index.js").write_text("needle", encoding="utf-8")
    (tmp_path / "model.gguf").write_bytes(b"GGUF\x00\x00\x00needle")
    (tmp_path / "app.min.js").write_text("x" * 5000 + "needle" + "y" * 5000, encoding="utf-8")
    return tmp_path


def test_find_files_sees_images_and_data_folders_but_not_clutter(tmp_path):
    ws = _ws(tmp_path)
    out = find_files("*.png", workspace_path=str(ws))
    assert "data/chart.png" in out and "modified" in out
    assert "build/" in find_files("build", workspace_path=str(ws))  # folders match too
    assert "index.js" not in find_files("*.js", workspace_path=str(ws))


def test_find_files_lists_newest_first(tmp_path):
    for i, name in enumerate(("old.md", "new.md")):
        (tmp_path / name).write_text("x", encoding="utf-8")
        os.utime(tmp_path / name, (time.time() - 1000 + i * 500,) * 2)
    out = find_files("*.md", workspace_path=str(tmp_path))
    assert out.index("new.md") < out.index("old.md")


def test_grep_skips_binaries_and_clutter_and_shows_context(tmp_path):
    ws = _ws(tmp_path)
    out = grep_in_files("needle", workspace_path=str(ws))
    assert "build/notes.txt" in out and "L1: alpha" in out and "> L2: needle here" in out and "L3: omega" in out
    assert "model.gguf" not in out and "index.js" not in out
    assert "binary or very large" in out
    minified = [line for line in out.splitlines() if "app.min.js" in line or line.startswith("  > L1: ...")]
    assert any(len(line) < 260 and "needle" in line for line in out.splitlines() if line.startswith("  > L1"))
    assert minified


def test_list_directory_groups_collapses_and_describes(tmp_path):
    ws = _ws(tmp_path)
    out = list_directory(".", workspace_path=str(ws))
    lines = out.splitlines()
    assert lines[0].startswith("Contents of '.' (3 folders, 2 files)")
    assert "node_modules/  (2 entries, not expanded)" in out
    assert max(i for i, l in enumerate(lines) if "[DIR]" in l) < min(i for i, l in enumerate(lines) if "[FILE]" in l)
    assert "app.min.js  (9.8 KB, modified" in out


def test_read_file_refuses_binaries_and_names_a_fallback_file(tmp_path):
    ws = _ws(tmp_path)
    assert "is a GGUF file" in read_file("model.gguf", workspace_path=str(ws))
    out = read_file("notes.txt", workspace_path=str(ws))
    assert out.startswith("[Nothing at 'notes.txt'; this is 'build/notes.txt']") and "needle here" in out


def test_patch_file_shows_the_closest_region_supports_replace_all_and_names_the_file(tmp_path):
    from app.agent.tools.patch_file import patch_file
    (tmp_path / "files").mkdir()
    target = tmp_path / "files" / "config.py"
    target.write_text("A = 1\nB = 2\nTIMEOUT = 30\nC = 3\nB = 2\n", encoding="utf-8")

    miss = patch_file("files/config.py", "TIMEOUT = 31", "TIMEOUT = 60", workspace_path=str(tmp_path))
    assert "closest part" in miss and "3: TIMEOUT = 30" in miss

    assert "Found 2 occurrences" in patch_file("files/config.py", "B = 2", "B = 5", workspace_path=str(tmp_path))
    out = patch_file("files/config.py", "B = 2", "B = 5", replace_all=True, workspace_path=str(tmp_path))
    assert "2 places" in out and target.read_text(encoding="utf-8").count("B = 5") == 2

    out = patch_file("config.py", "A = 1", "A = 9", workspace_path=str(tmp_path))
    assert "'files/config.py' (nothing was at 'config.py')" in out
