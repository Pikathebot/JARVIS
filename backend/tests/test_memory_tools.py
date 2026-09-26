"""
The model's own long-term memory: remember/forget, where each kind is scoped, what an ephemeral
chat may do, and the per-turn blocks that bring memories back (tagged with ids to act on).
"""
import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.agent.tools import memory as memory_tool
from app.agent.tools.memory import forget, remember, short_id
from app.agent.tools.registry import get_relevant_tools, TOOL_SCHEMAS
from app.agent.permissions import BASE_TOOL_RISK_MAP, RiskTier
from app.database.models import Memory
from app.memory.ephemeral import build_profile_context, build_project_memory_context


@pytest.fixture
def factory(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'mem.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    make = lambda: Session(engine)  # noqa: E731
    monkeypatch.setattr(memory_tool, "session_factory", make)
    return make


def _rows(factory):
    with factory() as db:
        return db.exec(select(Memory)).all()


def _id_in(result: str) -> str:
    return result.split("[", 1)[1].split("]", 1)[0]


def test_project_kinds_belong_to_the_project_and_user_kinds_are_global(factory):
    assert "for this project" in remember("Deploys go through the staging box first", kind="project", project_id="p1")
    assert "about the user" in remember("Works night shifts this month", kind="about_user", project_id="p1")
    by_content = {m.content: m for m in _rows(factory)}
    assert by_content["Deploys go through the staging box first"].project_id == "p1"
    assert by_content["Deploys go through the staging box first"].category == "project_preference"
    assert by_content["Works night shifts this month"].project_id is None


def test_project_kind_outside_a_workspace_is_kept_as_a_general_fact(factory):
    assert "about the user" in remember("The client's name is Acme.", kind="project", project_id=None)
    [row] = _rows(factory)
    assert (row.project_id, row.category) == (None, "recurring_fact")


@pytest.mark.parametrize("first,second,same", [
    ("User prefers all code examples written in Python", "User prefers all code examples written in Python.", True),
    ("User prefers all code examples written in Python.", "User wants all code examples written in Python.", True),
    ("User prefers code examples in Python", "User prefers code examples in Rust", False),
    ("User prefers British spelling in everything.", "Deploys go through staging first.", False),
])
def test_rewordings_count_as_the_same_memory(factory, first, second, same):
    remember(first, kind="preference")
    result = remember(second, kind="preference")
    assert result.startswith("Already remembered") == same
    assert len(_rows(factory)) == (1 if same else 2)


def test_ephemeral_chat_saves_and_forgets_nothing(factory):
    from app.memory.ephemeral import ephemeral_registry
    ephemeral_registry.get_or_create("eph-1", "WORKSPACE", None)
    try:
        assert remember("Likes tea", kind="preference", session_id="eph-1").startswith("Not saved")
        kept = remember("Likes coffee", kind="preference")
        assert forget(_id_in(kept), session_id="eph-1").startswith("Not changed")
        assert [m.content for m in _rows(factory)] == ["Likes coffee"]
    finally:
        ephemeral_registry.purge("eph-1")


def test_same_fact_twice_is_not_duplicated(factory):
    first = remember("Tabs, not spaces", kind="project", project_id="p1")
    again = remember("  tabs,  NOT spaces ", kind="project", project_id="p1")
    assert again.startswith(f"Already remembered as [{_id_in(first)}]")
    assert len(_rows(factory)) == 1


def test_replace_corrects_in_place_and_forget_deletes(factory):
    first = remember("The API runs on port 8000", kind="project", project_id="p1")
    mid = _id_in(first)
    assert remember("The API runs on port 9000", kind="project", replaces=mid, project_id="p1") == f"Updated memory [{mid}]."
    assert [m.content for m in _rows(factory)] == ["The API runs on port 9000"]
    assert forget(f"[{mid}]", project_id="p1").startswith(f"Forgot [{mid}]")
    assert _rows(factory) == []


def test_another_projects_memory_cannot_be_touched(factory):
    other = _id_in(remember("Secret codename is Falcon", kind="project", project_id="p2"))
    assert forget(other, project_id="p1").startswith("Error")
    assert remember("x", kind="project", replaces=other, project_id="p1").startswith("Error")
    assert len(_rows(factory)) == 1


def test_overlong_and_unknown_kinds_are_refused(factory):
    assert remember("x" * 400, kind="project", project_id="p1").startswith("Error")
    assert remember("fine", kind="gossip", project_id="p1").startswith("Error")


def test_limit_per_scope(factory, monkeypatch):
    monkeypatch.setattr(memory_tool, "MEMORY_LIMIT_PER_SCOPE", 2)
    remember("a1", kind="task", project_id="p1")
    remember("a2", kind="task", project_id="p1")
    assert remember("a3", kind="task", project_id="p1").startswith("Not saved")


def test_blocks_list_memories_with_their_ids(factory):
    proj = _id_in(remember("Chose SQLite over Postgres: one user, no server", kind="decision", project_id="p1"))
    user = _id_in(remember("Prefers short answers", kind="preference"))
    project_block = build_project_memory_context(factory, "p1")
    assert f"- [{proj}] (decision) Chose SQLite over Postgres" in project_block
    assert "Prefers short answers" not in project_block
    assert build_project_memory_context(factory, "p2") == ""
    assert f"- [{user}] Prefers short answers" in build_profile_context(factory)


def test_project_memories_ride_on_a_workspace_turn(factory, tmp_path):
    from app.agent.orchestrator import AgentOrchestrator
    remember("Release notes live in CHANGES.md", kind="project", project_id="p1")

    class Store:
        _session_factory = staticmethod(factory)

    orch = AgentOrchestrator.__new__(AgentOrchestrator)
    orch.memory_store = Store()
    assert "Release notes live in CHANGES.md" in orch._space_context("WORKSPACE", "p1", tmp_path)
    assert "Release notes live in CHANGES.md" not in orch._space_context("FREEFORM", None, None)


def test_memory_tools_are_offered_every_turn_and_run_unconfirmed():
    for message, mode in (("hi", "WORKSPACE"), ("what's the weather like", "FREEFORM"), ("read main.py", "WORKSPACE")):
        names = {t.__name__ for t in get_relevant_tools(message, chat_mode=mode)}
        assert {"remember", "forget"} <= names, (message, names)
    assert BASE_TOOL_RISK_MAP["remember"] == RiskTier.LOW_RISK
    assert BASE_TOOL_RISK_MAP["forget"] == RiskTier.LOW_RISK
    schema = TOOL_SCHEMAS["remember"].model_json_schema()
    assert set(schema["properties"]["kind"]["enum"]) == set(memory_tool.MEMORY_KINDS)


@pytest.mark.parametrize("message", [
    "let's keep every prompt under 200 words",
    "from now on, always write the examples in Python",
    "remember that the client's name is Acme",
    "I prefer British spelling in everything",
    "we decided to drop the v1 API",
    "my name is Sam",
    "can you remember that I'm off on Fridays?",
])
def test_lasting_statements_cue_a_save(message):
    from app.agent.orchestrator import memory_cue
    assert memory_cue(message) == "save"


@pytest.mark.parametrize("message", ["what's the capital of France", "hi", "read main.py", "how do I sort a list",
                                     "does python always pass by reference?", "should I always use pathlib?"])
def test_ordinary_messages_cue_nothing(message):
    from app.agent.orchestrator import memory_cue
    assert memory_cue(message) == ""


def test_forget_requests_cue_a_forget():
    from app.agent.orchestrator import memory_cue
    assert memory_cue("forget that I said I'm vegetarian") == "forget"


def test_turn_directive_goes_last_outside_the_context_bracket(factory):
    from app.memory.context_manager import ContextManager
    from app.memory.store import MemoryStore
    pkg = ContextManager(memory_store=MemoryStore(session_factory=factory)).build_context(
        user_message="let's keep every prompt under 200 words",
        turn_context="CURRENT TIME: now",
        retrieved_chunks=[{"chunk_id": "c", "content": "chunk text", "file_path": "a.md", "similarity_score": 0.9}],
        turn_directive="MEMORY: save it",
    )
    last = pkg.messages[-1]["content"]
    assert last.startswith("let's keep every prompt under 200 words")
    assert last.endswith("]\n\nMEMORY: save it")


class _ChoosingProvider:
    """Answers the capture request with one forced tool call, and records how it was asked."""
    def __init__(self, call):
        self.call, self.kwargs = call, None

    async def chat(self, **kwargs):
        self.kwargs = kwargs
        return {"message": {"role": "assistant", "content": "", "tool_calls": [self.call] if self.call else []}}


@pytest.mark.asyncio
async def test_capture_saves_what_the_model_chooses(factory):
    from app.agent.memory_capture import capture_memory
    provider = _ChoosingProvider({"id": "c1", "type": "function", "function": {
        "name": "remember", "arguments": {"content": "Keep every prompt under 200 words.", "kind": "project"}}})
    result = await capture_memory(provider, "main", "let's keep every prompt under 200 words",
                                  {"project_id": "p1", "session_id": "s1"})
    assert result.startswith("Remembered")
    assert [(m.content, m.project_id) for m in _rows(factory)] == [("Keep every prompt under 200 words.", "p1")]
    assert provider.kwargs["tool_choice"] == "required" and provider.kwargs["thinking"] is False


@pytest.mark.asyncio
async def test_capture_skip_saves_nothing(factory):
    from app.agent.memory_capture import capture_memory
    provider = _ChoosingProvider({"id": "c1", "type": "function", "function": {"name": "nothing_to_remember", "arguments": {}}})
    assert await capture_memory(provider, "main", "does python always pass by reference?", {"project_id": "p1"}) is None
    assert _rows(factory) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("message,expect_capture", [
    ("let's keep every prompt under 200 words", True),
    ("what's the capital of France", False),
])
async def test_stream_turn_starts_capture_only_after_an_unsaved_lasting_message(monkeypatch, message, expect_capture):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock
    from app.agent import orchestrator as orch_mod
    from tests.fake_providers import ClientBackedProvider

    captured = []

    async def fake_capture(provider, model, user_message, tool_context):
        captured.append(user_message)

    monkeypatch.setattr(orch_mod, "capture_memory", fake_capture)
    store = MagicMock()
    store.get_or_create_session.return_value = {"chat_mode": "WORKSPACE"}
    store.get_messages.return_value = []
    skills = MagicMock()
    skills.match_skills.return_value = []
    skills.build_skill_prompt_injection.return_value = ""
    mcp = MagicMock()
    mcp.get_tool_definitions.return_value = []
    client = MagicMock()

    async def reply(*args, **kwargs):
        yield {"type": "token", "delta": "Understood, sir."}
        yield {"type": "done", "content": "Understood, sir.", "tool_calls": []}

    client.chat_stream = reply
    orch = orch_mod.AgentOrchestrator(provider=ClientBackedProvider(client), openrouter_client=MagicMock(),
                                      memory_store=store, compactor=MagicMock(compact=AsyncMock(return_value=([], None))),
                                      skills_loader=skills, mcp_manager=mcp)
    events = [ev async for ev in orch.run_stream(user_message=message, session_id="s-cap")]
    await asyncio.sleep(0)
    assert "done" in [e.get("event") for e in events]
    assert captured == ([message] if expect_capture else [])
