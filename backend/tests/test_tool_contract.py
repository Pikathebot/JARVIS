"""
The offered-tool contract (PLAN 4.13 #3). The tool list is fixed per space so the prompt prefix stays
cached, which means a tool can be offered where it cannot run. That is fine only if it then refuses
cleanly: an "Error: ..." string saying why and not to retry, never an exception, a hang, or a raw
"fatal: ..." from a subprocess. Tools with real side effects (launching apps, typing, killing
processes) are only walked where they are refused before running.
"""
import re
from pathlib import Path

import pytest

from app.agent.orchestrator import TOOL_PROTOCOL_RULES
from app.agent.permissions import BASE_TOOL_RISK_MAP
from app.agent.tool_schema import convert_tool_to_openai_schema
from app.agent.tools import git as git_tools
from app.agent.tools.registry import (
    TOOL_FUNCTIONS, TOOL_SCHEMAS, WORKSPACE_BOUND_TOOLS, execute_tool, tools_for_space,
)
from app.skills.loader import SkillsLoader

GIT_TOOLS = sorted(n for n in TOOL_FUNCTIONS if n.startswith("git_"))
# The smallest arguments each git tool accepts; the refusal must come before they matter.
GIT_ARGS = {"git_commit": {"message": "x"}, "git_switch": {"branch": "b"}, "git_restore": {"file_path": "a.txt"}}


@pytest.mark.parametrize("space", ["FREEFORM", "WORKSPACE"])
def test_every_offered_tool_is_registered_tiered_and_described(space):
    for fn in tools_for_space(space):
        name = fn.__name__
        assert name in TOOL_FUNCTIONS, f"{name} offered but not executable"
        assert name in BASE_TOOL_RISK_MAP, f"{name} has no explicit risk tier"
        assert name in TOOL_SCHEMAS or not _has_params(fn), f"{name} takes arguments but has no schema"


def _has_params(fn):
    import inspect
    runtime = {"workspace_path", "project_id", "session_id", "user_message", "allow_outside", "context"}
    return any(p not in runtime for p in inspect.signature(fn).parameters)


@pytest.mark.parametrize("name", sorted(WORKSPACE_BOUND_TOOLS))
def test_workspace_tools_refuse_cleanly_in_freeform(name, tmp_path):
    out = execute_tool(name, {}, {"chat_mode": "FREEFORM", "workspace_path": str(tmp_path)})
    assert out.startswith("Error:") and "Freeform" in out
    assert not any(tmp_path.iterdir()), "a refused tool must not touch anything"


@pytest.mark.parametrize("name", GIT_TOOLS)
def test_git_tools_refuse_cleanly_outside_a_repository(name, tmp_path):
    out = execute_tool(name, GIT_ARGS.get(name, {}), {"chat_mode": "WORKSPACE", "workspace_path": str(tmp_path)})
    assert out.startswith("Error: not available here") and "not a git repository" in out and "Don't retry" in out


def test_git_tools_still_work_inside_a_repository(tmp_path):
    code, _ = git_tools._git(["init"], tmp_path)
    if code != 0:
        pytest.skip("git not available")
    out = execute_tool("git_status", {}, {"chat_mode": "WORKSPACE", "workspace_path": str(tmp_path)})
    assert not out.startswith("Error"), out


# --- harness lint (PLAN 4.15 #6, after harness2's "validate before any run"): a rule or skill that
# names a tool which doesn't exist, or a parameter the schema doesn't have, steers the model into
# a call that can only fail. Nothing in the app checks this at runtime, so the tests do.

_RULE_CALL = re.compile(r"'([a-z_]+)\(([^)]*)\)'")
_RULE_NAME = re.compile(r"'([a-z]+_[a-z_]+)'")
# Named by the shared rules but Workspace-only, so absent from Freeform's tool list. Freeform's
# execute_tool refuses them with "switch to a Workspace". Add a tool here only on purpose.
_NAMED_BUT_WORKSPACE_ONLY = {"delete_file", "execute_command",
                             # Freeform's file tools trimmed to three (2026-10-18, eval 4.15)
                             "list_directory", "grep_in_files", "patch_file"}


def _schema_params(name):
    schema = convert_tool_to_openai_schema(TOOL_FUNCTIONS[name]) or {}
    return set(schema.get("function", {}).get("parameters", {}).get("properties", {}))


def test_rules_call_only_real_tools_with_real_parameters():
    calls = _RULE_CALL.findall(TOOL_PROTOCOL_RULES)
    assert calls, "the rules' call syntax changed; update _RULE_CALL"
    for name, args in calls:
        assert name in TOOL_FUNCTIONS, f"TOOL_PROTOCOL_RULES names '{name}', which isn't a tool"
        params = [a.split("=", 1)[0].strip() for a in args.split(",") if "=" in a]
        missing = [p for p in params if p not in _schema_params(name)]
        assert not missing, f"rules call {name}({', '.join(missing)}=...), not in its schema"


@pytest.mark.parametrize("space", ["FREEFORM", "WORKSPACE"])
def test_rules_name_only_tools_the_space_offers(space):
    named = {n for n, _ in _RULE_CALL.findall(TOOL_PROTOCOL_RULES)}
    named |= set(_RULE_NAME.findall(TOOL_PROTOCOL_RULES)) & set(TOOL_FUNCTIONS)
    offered = {fn.__name__ for fn in tools_for_space(space)}
    allowed = _NAMED_BUT_WORKSPACE_ONLY if space == "FREEFORM" else set()
    assert named - offered <= allowed, f"{space}: rules name tools it doesn't offer: {sorted(named - offered - allowed)}"
    assert allowed <= WORKSPACE_BOUND_TOOLS


def test_skills_parse_with_triggers_and_name_real_tools():
    skills = SkillsLoader().discover_skills()
    assert skills, "no skills found under skills/"
    seen: dict[str, str] = {}
    for s in skills:
        assert s.description.strip(), f"skill {s.name} has no description"
        assert [t for t in s.triggers if str(t).strip()], f"skill {s.name} has no triggers, so it never loads"
        for trig in s.triggers:
            key = str(trig).strip().lower()
            assert key not in seen, f"trigger '{trig}' is in both {seen.get(key)} and {s.name}"
            seen[key] = s.name
        unknown = [t for t in s.tools if t not in TOOL_FUNCTIONS]
        assert not unknown, f"skill {s.name} lists tools that don't exist: {unknown}"
        body = Path(s.file_path).read_text(encoding="utf-8")
        dotted = re.findall(r"`([a-z_]+\.[a-z_]+)`", body)
        assert not dotted, f"skill {s.name} tells the model to use {dotted}; no such tools"
