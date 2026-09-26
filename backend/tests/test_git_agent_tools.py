"""
Git tools and ranged read_file in the live agent tool set (app/agent/tools).

The git tools used to exist only as BaseTools in app/tools, which nothing ever offered to the
model. These pin that they are now registered, schema'd, tiered and triggered like every other
live tool, and that they behave against a real repository.
"""
import shutil
import subprocess

import pytest

from app.agent.permissions import RiskTier, build_confirmation_prompt, evaluate_tool_permission
from app.agent.tool_schema import convert_tool_to_openai_schema
from app.agent.tools.git import git_checkout, git_commit, git_diff, git_log, git_status
from app.agent.tools.read_file import read_file
from app.agent.tools.registry import AVAILABLE_TOOLS, TOOL_FUNCTIONS, execute_tool, get_relevant_tools

GIT_TOOLS = ("git_status", "git_diff", "git_log", "git_commit", "git_checkout")

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _run(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    _run(tmp_path, "init", "-q", "-b", "main")
    _run(tmp_path, "config", "user.name", "Test")
    _run(tmp_path, "config", "user.email", "test@example.com")
    _run(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / "a.txt").write_text("one\n", encoding="utf-8")
    _run(tmp_path, "add", "-A")
    _run(tmp_path, "commit", "-q", "-m", "first")
    return tmp_path


# --- registration ---------------------------------------------------------------------------

def test_git_tools_are_registered_offered_and_schemad():
    offered = {getattr(t, "__name__", "") for t in AVAILABLE_TOOLS}
    for name in GIT_TOOLS:
        assert name in TOOL_FUNCTIONS
        assert name in offered
    commit = convert_tool_to_openai_schema(TOOL_FUNCTIONS["git_commit"])["function"]
    assert commit["parameters"]["required"] == ["message"]
    # Only the docstring's first line reaches the model, so the caution has to be on it.
    assert "only when the user explicitly asks" in commit["description"]


def test_reads_are_free_and_writes_need_approval():
    for name in ("git_status", "git_diff", "git_log"):
        assert evaluate_tool_permission(name, {}).allowed
    for name, args in (("git_commit", {"message": "x"}), ("git_checkout", {"target": "main"})):
        decision = evaluate_tool_permission(name, args)
        assert not decision.allowed
        assert decision.risk_tier == RiskTier.CONFIRMATION_REQUIRED


def test_confirmation_prompt_names_the_commit_message():
    prompt = build_confirmation_prompt(
        [{"tool": "git_commit", "args": {"message": "fix typo"}, "risk_tier": "CONFIRMATION_REQUIRED"}],
        persona=None,
    )
    assert "git commit: fix typo" in prompt["spoken"]


@pytest.mark.parametrize("query", [
    "what did I change today?", "commit this as fix typo", "show me the git log",
    "which branch am I on", "any uncommitted work?",
])
def test_git_questions_offer_git_tools(query):
    names = {t.__name__ for t in get_relevant_tools(query)}
    assert set(GIT_TOOLS) <= names


@pytest.mark.parametrize("query", [
    "write a report", "the colors are different", "log in to discord", "a commitment to quality",
])
def test_lookalike_words_do_not_offer_git_tools(query):
    names = {t.__name__ for t in get_relevant_tools(query)}
    assert not names & set(GIT_TOOLS)


# --- behaviour against a real repository ----------------------------------------------------

@needs_git
def test_status_diff_and_log(repo):
    assert "Working tree clean" in git_status(workspace_path=str(repo))
    (repo / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
    assert "a.txt" in git_status(workspace_path=str(repo))
    assert "+two" in git_diff(workspace_path=str(repo))
    assert git_diff(staged=True, workspace_path=str(repo)) == "No staged changes."
    log = git_log(workspace_path=str(repo))
    assert "first" in log and "Test" in log


@needs_git
def test_commit_stages_everything_and_keeps_the_message_literal(repo):
    assert "Nothing to commit" in git_commit("noop", workspace_path=str(repo))
    (repo / "b.txt").write_text("new\n", encoding="utf-8")
    message = 'say "hi" && echo pwned'
    git_commit(message, workspace_path=str(repo))
    assert "Working tree clean" in git_status(workspace_path=str(repo))
    assert git_log(limit=1, workspace_path=str(repo)).endswith(message)


@needs_git
def test_commit_with_files_commits_only_those(repo):
    (repo / "keep.txt").write_text("mine\n", encoding="utf-8")
    (repo / "other.txt").write_text("not mine\n", encoding="utf-8")
    _run(repo, "add", "other.txt")  # already staged by someone else: must stay uncommitted
    out = git_commit("only keep", files=["keep.txt"], workspace_path=str(repo))
    assert not out.startswith("Error"), out
    committed = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"], cwd=repo,
                               check=True, capture_output=True, text=True).stdout
    assert "keep.txt" in committed and "other.txt" not in committed
    assert "other.txt" in git_status(workspace_path=str(repo))
    assert "no changes" in git_commit("again", files=["keep.txt"], workspace_path=str(repo))
    # A path that looks like an option is still just a path.
    assert git_commit("x", files=["--amend"], workspace_path=str(repo)).startswith("Error")


@needs_git
def test_checkout_switches_branch_and_refuses_options(repo):
    _run(repo, "branch", "feature")
    git_checkout("feature", workspace_path=str(repo))
    assert "feature" in git_status(workspace_path=str(repo)).splitlines()[0]
    assert git_checkout("--force", workspace_path=str(repo)).startswith("Error")


@needs_git
def test_repo_path_cannot_leave_the_workspace(repo):
    assert "Access denied" in git_status(repo_path="..", workspace_path=str(repo))


@needs_git
def test_execute_tool_injects_the_workspace(repo):
    out = execute_tool("git_log", {"limit": 1}, context={"workspace_path": str(repo)})
    assert "first" in out


# --- read_file line ranges ------------------------------------------------------------------

def test_read_file_whole_file_is_unchanged(tmp_path):
    (tmp_path / "f.txt").write_text("a\nb\nc\n", encoding="utf-8")
    assert read_file("f.txt", workspace_path=str(tmp_path)) == "a\nb\nc\n"


def test_read_file_range_is_labelled_so_the_model_can_page(tmp_path):
    (tmp_path / "f.txt").write_text("".join(f"line {i}\n" for i in range(1, 11)), encoding="utf-8")
    out = read_file("f.txt", start_line=3, end_line=4, workspace_path=str(tmp_path))
    assert out == "[Lines 3-4 of 10 in 'f.txt']\nline 3\nline 4\n"
    assert read_file("f.txt", start_line=9, workspace_path=str(tmp_path)).endswith("line 10\n")
    assert "past the end" in read_file("f.txt", start_line=50, workspace_path=str(tmp_path))
    assert "before start_line" in read_file("f.txt", start_line=5, end_line=2, workspace_path=str(tmp_path))


def test_read_file_schema_offers_the_range():
    props = convert_tool_to_openai_schema(TOOL_FUNCTIONS["read_file"])["function"]["parameters"]["properties"]
    assert {"start_line", "end_line"} <= set(props)
