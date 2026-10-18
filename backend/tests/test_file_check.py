"""The check after a write (PLAN 4.15 #5): read back, parse what can be parsed, one sentence in the result."""
from app.agent.tools.file_check import check_written
from app.agent.tools.patch_file import patch_file
from app.agent.tools.write_file import write_file


def test_valid_json_is_confirmed_and_broken_json_is_flagged_with_its_line(tmp_path):
    ok = write_file("data/a.json", '{"a": 1}', workspace_path=str(tmp_path))
    assert ok.startswith("Successfully wrote") and ok.endswith("Checked: valid JSON.")

    bad = write_file("data/b.json", '{"a": 1,\n "b": }', workspace_path=str(tmp_path))
    assert bad.startswith("Successfully wrote"), "the write happened; the warning must not read as a failure"
    assert "not valid JSON" in bad and "line 2" in bad


def test_python_syntax_error_names_the_line(tmp_path):
    out = write_file("tool.py", "def f():\n    return 1\nprint(f(\n", workspace_path=str(tmp_path))
    assert "Python syntax error at line" in out
    assert "the Python compiles" in write_file("ok.py", "x = 1\n", workspace_path=str(tmp_path))


def test_csv_with_a_ragged_row_is_flagged(tmp_path):
    out = write_file("t.csv", "name,size\na,1\nb,2,extra\n", workspace_path=str(tmp_path))
    assert "row 3 has 3 columns but the header has 2" in out
    assert "3 rows, 2 columns each" in write_file("u.csv", "name,size\na,1\nb,2\n", workspace_path=str(tmp_path))


def test_yaml_toml_and_jsonl(tmp_path):
    assert "not valid YAML" in write_file("c.yaml", "a: [1, 2\n", workspace_path=str(tmp_path))
    assert "valid TOML" in write_file("c.toml", 'a = "x"\n', workspace_path=str(tmp_path))
    assert "line 2 is not valid JSON" in write_file("c.jsonl", '{"a": 1}\n{oops}\n', workspace_path=str(tmp_path))


def test_plain_text_gets_no_sentence(tmp_path):
    out = write_file("notes.md", "# Notes\n\nline\r\nother\n", workspace_path=str(tmp_path))
    assert out.endswith("lines) to 'notes.md'.")


def test_a_file_that_differs_from_what_was_written_is_flagged(tmp_path):
    p = tmp_path / "x.txt"
    p.write_text("something else", encoding="utf-8")
    assert "doesn't match what was written" in check_written(p, "the intended text")
    assert check_written(p, "something else") == ""


def test_patch_result_carries_the_check(tmp_path):
    (tmp_path / "cfg.json").write_text('{"port": 8000}', encoding="utf-8")
    out = patch_file("cfg.json", '"port": 8000', '"port": ', workspace_path=str(tmp_path))
    assert out.startswith("Successfully patched") and "not valid JSON" in out


def test_checker_failure_says_nothing(tmp_path):
    assert check_written(tmp_path / "missing.json") == ""
