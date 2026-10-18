"""The eval's own helpers (PLAN 4.15): result files and run stamps, the contrast report, and the
unsourced-specifics diagnostic. Stdlib scripts under scripts/, no model needed."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import eval_contrast as ec  # noqa: E402
import eval_grounding as eg  # noqa: E402
import eval_io  # noqa: E402


def test_result_files_round_trip_and_old_lists_still_load(tmp_path):
    new = tmp_path / "new.json"
    eval_io.write_results(new, {"head_sha": "a"}, [{"id": "t", "success": True}])
    assert eval_io.load_results(new) == ({"head_sha": "a"}, [{"id": "t", "success": True}])
    old = tmp_path / "old.json"
    old.write_text('[{"id": "t", "success": false}]', encoding="utf-8")
    assert eval_io.load_results(old) == ({}, [{"id": "t", "success": False}])


def test_compare_meta_names_what_differs():
    a = {"model_file": "m.gguf", "head_sha": "1", "git": {"commit": "abc", "dirty_backend": []}}
    b = {"model_file": "m.gguf", "head_sha": "2", "git": {"commit": "abc", "dirty_backend": ["backend/app/x.py"]}}
    lines = eval_io.compare_meta(a, b)
    assert any(line.startswith("head_sha") for line in lines)
    assert any("abc+dirty" in line for line in lines)
    assert eval_io.compare_meta(a, dict(a)) == []
    assert eval_io.compare_meta(a, {})  # an unstamped file is flagged, not silently compared


def _trace(rep, rounds):
    return {"rep": rep, "turns": [{"pre": [], "rounds": rounds, "final": ""}]}


def test_divergence_names_the_first_step_where_runs_part():
    passed = _trace(1, [{"calls": [{"name": "web_search", "args": {"q": "a"}}], "net": ""},
                        {"calls": [{"name": "fetch_url", "args": {"url": "u"}}], "net": ""},
                        {"calls": [], "net": "", "content": "Wukong Together"}])
    failed = _trace(0, [{"calls": [{"name": "web_search", "args": {"q": "a"}}], "net": ""},
                        {"calls": [], "net": "", "content": "Probably Seamless Co-op"}])
    out = ec.divergence(passed, failed)
    assert out.startswith("step 2 (different step)") and "fetch_url" in out and "Seamless" in out
    same_tool = _trace(0, [{"calls": [{"name": "web_search", "args": {"q": "b"}}], "net": ""}])
    assert "same tool, other arguments" in ec.divergence(passed, same_tool)


def test_unsourced_flags_invented_specifics_not_text_shape():
    src = ["Studio Pierrot animates Bleach. OpenAPI and JSON Schema docs. Tamil Nadu headlines"]
    assert eg.unsourced("Studio Pierrot’s new arc. See OpenAPI and JSON Schema. Current Tamil Nadu news.", src) == []
    assert eg.unsourced("It's Wukong Together, with about 12,000 downloads.",
                        ["Wukong Together (v3.2): 12,000 downloads"]) == []
    assert eg.unsourced("Ishiguro won in 2003.", ["Ishiguro won the Nobel Prize in 2017."]) == ["2003"]
    assert eg.unsourced("The best is Seamless Coop Plus, rated 4.8.", ["results"]) == ["Seamless Coop Plus", "4.8"]
    assert eg.unsourced("Use 3 tabs and 2 spaces.", []) == []  # single digits are not specifics
