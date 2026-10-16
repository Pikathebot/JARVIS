"""
The offered-tool contract (PLAN 4.13 #3). The tool list is fixed per space so the prompt prefix stays
cached, which means a tool can be offered where it cannot run. That is fine only if it then refuses
cleanly: an "Error: ..." string saying why and not to retry, never an exception, a hang, or a raw
"fatal: ..." from a subprocess. Tools with real side effects (launching apps, typing, killing
processes) are only walked where they are refused before running.
"""
import pytest

from app.agent.permissions import BASE_TOOL_RISK_MAP
from app.agent.tools import git as git_tools
from app.agent.tools.registry import (
    TOOL_FUNCTIONS, TOOL_SCHEMAS, WORKSPACE_BOUND_TOOLS, execute_tool, tools_for_space,
)

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
