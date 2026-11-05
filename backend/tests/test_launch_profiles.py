"""PLAN 4.28: the launch profile table -- parsing, validation, and which bucket the free VRAM picks."""
import json

import pytest

from app.agent.launch_profiles import AS_CONFIGURED, BUILT_IN, TableError, load_table, parse_table


def _table(*profiles, default=None):
    data = {"profiles": list(profiles)}
    if default:
        data["default"] = default
    return parse_table(data)


def test_without_a_file_the_one_profile_sets_nothing(tmp_path):
    table = load_table(tmp_path / "absent.json")
    assert table is BUILT_IN and table.profiles == (AS_CONFIGURED,) and table.error is None
    assert AS_CONFIGURED.to_dict() == {"name": "as configured", "min_free_mb": 0.0}
    for free in (None, 0, 500, 7000, 24000):
        assert table.pick(free) is AS_CONFIGURED


def test_the_highest_edge_the_card_reaches_wins_and_unknown_vram_takes_the_default():
    table = _table({"name": "small", "min_free_mb": 0, "main_ctx": 8192},
                   {"name": "big", "min_free_mb": 12000, "main_ctx": 49152},
                   {"name": "laptop", "min_free_mb": 6000}, default="laptop")
    assert [table.pick(f).name for f in (0, 5999, 6000, 11999.5, 12000, 30000)] == \
        ["small", "small", "laptop", "laptop", "big", "big"]
    assert table.pick(None).name == "laptop"
    assert _table({"name": "a", "min_free_mb": 4000}, {"name": "b", "min_free_mb": 8000}).pick(None).name == "a"
    # Below every edge: the smallest bucket, the likeliest to fit.
    assert _table({"name": "a", "min_free_mb": 4000}, {"name": "b", "min_free_mb": 8000}).pick(1000).name == "a"


@pytest.mark.parametrize("data, says", [
    ({}, "at least one profile"),
    ({"profiles": []}, "at least one profile"),
    ({"profiles": [{"min_free_mb": 0}]}, "needs a name"),
    ({"profiles": [{"name": "x", "main_ctx": 100}]}, "main_ctx"),
    ({"profiles": [{"name": "x", "main_ctx": "32k"}]}, "main_ctx"),
    ({"profiles": [{"name": "x", "min_free_mb": -1}]}, "min_free_mb"),
    ({"profiles": [{"name": "x", "projector": "no"}]}, "projector"),
    ({"profiles": [{"name": "x", "ctx": 8192}]}, "unknown field"),
    ({"profiles": [{"name": "x"}, {"name": "x", "min_free_mb": 9}]}, "same name"),
    ({"profiles": [{"name": "x"}, {"name": "y"}]}, "same min_free_mb"),
    ({"profiles": [{"name": "x"}], "default": "y"}, "not one of the profiles"),
])
def test_a_bad_table_is_refused_with_a_reason(data, says):
    with pytest.raises(TableError, match=says):
        parse_table(data)


def test_a_bad_file_is_ignored_and_says_why(tmp_path):
    path = tmp_path / "launch_profiles.json"
    path.write_text("{not json", encoding="utf-8")
    table = load_table(path)
    assert table.profiles == (AS_CONFIGURED,) and "launch_profiles.json was ignored" in table.error
    path.write_text(json.dumps({"profiles": [{"name": "x", "main_ctx": 1}]}), encoding="utf-8")
    assert "main_ctx" in load_table(path).error
    path.write_text(json.dumps({"profiles": [{"name": "x", "main_ctx": 8192}]}), encoding="utf-8")
    good = load_table(path)
    assert good.error is None and good.source == str(path) and good.pick(None).main_ctx == 8192
