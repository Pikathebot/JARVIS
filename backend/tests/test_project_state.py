"""A project's living state, .jarvis/PROJECT.md (PLAN 4.18 phase 1)."""
import uuid
from datetime import datetime

import httpx
import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from fake_providers import use_model_client
from app import project_state as ps
from app.database.models import Memory, Project


@pytest.fixture
def ws(tmp_path):
    folder = tmp_path / "my-project"
    folder.mkdir()
    return str(folder)


def _sections(ws, pid="p1"):
    _, sections = ps.parse(ps.read(ws, pid))
    return {h: [line for line in body if line.strip()] for h, body in sections}


def test_template_has_the_five_sections_and_lives_in_the_folder(ws):
    path = ps.ensure(ws, "p1", "Game", "A small platformer")
    assert path == ps.state_path(ws, "p1")
    assert str(path).endswith(".jarvis\\PROJECT.md") or str(path).endswith(".jarvis/PROJECT.md")
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# Game\n")
    assert [h for h, _ in ps.parse(text)[1]] == list(ps.SECTIONS)
    assert _sections(ws)["Goal"] == ["A small platformer"]


def test_each_action_edits_its_own_section(ws):
    ps.ensure(ws, "p1", "Game")
    d = "2026-10-25"
    assert ps.edit(ws, "p1", "Game", "set_now", "Player movement", today=d) == "Now set."
    assert ps.edit(ws, "p1", "Game", "set_now", "Jumping", today=d) == "Now set."
    assert ps.edit(ws, "p1", "Game", "add_next", "- [ ] Add a jump sound", today=d) == "Added to Next."
    assert ps.edit(ws, "p1", "Game", "add_next", "Add a jump sound", today=d) == "Already in Next."
    assert ps.edit(ws, "p1", "Game", "add_next", "Level two", today=d) == "Added to Next."
    assert ps.edit(ws, "p1", "Game", "tick", "jump sound", today=d) == "Ticked off and moved to Done."
    assert ps.edit(ws, "p1", "Game", "add_done", "2026-01-01 Camera follows the player", today=d) == "Added to Done."
    assert ps.edit(ws, "p1", "Game", "add_decision", "Godot over Unity: lighter on the laptop", today=d) == "Added to Decisions."
    s = _sections(ws)
    assert s["Now"] == ["Jumping"]
    assert s["Next"] == ["- [ ] Level two"]
    # newest first; the model's own date and list marker are dropped
    assert s["Done"] == [f"- {d} Camera follows the player", f"- {d} Add a jump sound"]
    assert s["Decisions"] == [f"- {d} Godot over Unity: lighter on the laptop"]


def test_tick_that_matches_nothing_lists_what_is_there(ws):
    ps.ensure(ws, "p1", "Game")
    ps.edit(ws, "p1", "Game", "add_next", "Level two")
    out = ps.edit(ws, "p1", "Game", "tick", "boss fight")
    assert out.startswith("Error:") and "Level two" in out


def test_bad_input_changes_nothing(ws):
    ps.ensure(ws, "p1", "Game")
    before = ps.read(ws, "p1")
    assert ps.edit(ws, "p1", "Game", "rewrite", "x").startswith("Error:")
    assert ps.edit(ws, "p1", "Game", "add_done", "   ").startswith("Error:")
    assert ps.edit(ws, "p1", "Game", "add_done", "x" * 300).startswith("Error:")
    assert ps.read(ws, "p1") == before


def test_hand_edits_and_unknown_sections_survive_an_edit(ws):
    path = ps.ensure(ws, "p1", "Game")
    text = path.read_text(encoding="utf-8").replace("## Now\n", "## Now\nMy own words.\n")
    path.write_text(text + "\n## Links\n- https://example.com\n", encoding="utf-8")
    ps.edit(ws, "p1", "Game", "add_decision", "Pixel art")
    s = _sections(ws)
    assert s["Now"] == ["My own words."]
    assert s["Links"] == ["- https://example.com"]


def test_the_cap_trims_the_oldest_done_first_and_never_decisions(ws):
    ps.ensure(ws, "p1", "Game")
    for i in range(12):
        ps.edit(ws, "p1", "Game", "add_decision", f"decision {i} " + "d" * 60, today="2026-10-01")
    for i in range(30):
        assert not ps.edit(ws, "p1", "Game", "add_done", f"done {i} " + "x" * 40, today="2026-10-02").startswith("Error")
    text = ps.read(ws, "p1")
    assert len(text) <= ps.STATE_MAX_CHARS
    s = _sections(ws)
    assert len(s["Decisions"]) == 12
    assert "done 29" in s["Done"][0] and not any("done 0 " in line for line in s["Done"])


def test_a_full_file_refuses_instead_of_dropping_decisions(ws):
    ps.ensure(ws, "p1", "Game")
    out = ""
    for i in range(40):
        out = ps.edit(ws, "p1", "Game", "add_decision", f"decision {i} " + "d" * 80)
        if out.startswith("Error"):
            break
    assert "full" in out
    assert len(ps.read(ws, "p1")) <= ps.STATE_MAX_CHARS


def test_every_write_is_backed_up(ws):
    ps.ensure(ws, "p1", "Game")
    ps.edit(ws, "p1", "Game", "set_now", "one")
    ps.edit(ws, "p1", "Game", "set_now", "two")
    backups = list((ps.workspace_dir(ws, "p1") / ".jarvis" / "backups").rglob("PROJECT.md*"))
    assert backups


def test_head_block_and_the_per_chat_snapshot(ws):
    ps.ensure(ws, "p1", "Game")
    block = ps.head_block("Answer in British English.", ps.read(ws, "p1"))
    assert "--- Project Instructions ---\nAnswer in British English." in block
    assert "--- Project State" in block and "## Next" in block
    calls = []
    first = ps.snapshot("chat-a", lambda: calls.append(1) or "v1")
    again = ps.snapshot("chat-a", lambda: calls.append(1) or "v2")
    assert (first, again, len(calls)) == ("v1", "v1", 1)
    ps.drop_snapshot("chat-a")
    assert ps.snapshot("chat-a", lambda: "v2") == "v2"
    ps.drop_snapshot("chat-a")
    assert ps.head_block(None, "") == ""


def test_decision_and_task_memories_move_into_the_file_once(ws, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    factory = lambda: Session(engine)  # noqa: E731
    now = datetime(2026, 10, 20)
    with factory() as db:
        db.add(Project(id="p1", name="Game", workspace_path=ws, is_active=False, created_at=now, updated_at=now))
        for cat, text in (("important_decision", "Godot, not Unity"), ("persistent_task_context", "Finish level two"),
                          ("project_preference", "Tabs, not spaces")):
            db.add(Memory(id=str(uuid.uuid4()), project_id="p1", category=cat, content=text,
                          created_at=now, updated_at=now))
        db.commit()
    ps.ensure_with_memories(factory, ws, "p1", "Game")
    s = _sections(ws)
    assert s["Decisions"] == ["- 2026-10-20 Godot, not Unity"]
    assert s["Next"] == ["- [ ] Finish level two"]
    with factory() as db:
        left = [m.category for m in db.exec(select(Memory)).all()]
    assert left == ["project_preference"]
    # a second call finds the file and leaves everything alone
    ps.ensure_with_memories(factory, ws, "p1", "Game")
    assert _sections(ws)["Decisions"] == ["- 2026-10-20 Godot, not Unity"]


def test_the_tool_needs_a_project_and_routes_to_the_file(ws):
    from app.agent.tools.registry import execute_tool, tools_for_space, WORKSPACE_BOUND_TOOLS
    from app.agent.permissions import BASE_TOOL_RISK_MAP, RiskTier

    assert "project_state" in WORKSPACE_BOUND_TOOLS
    assert "project_state" not in [t.__name__ for t in tools_for_space("FREEFORM")]
    assert BASE_TOOL_RISK_MAP["project_state"] == RiskTier.LOW_RISK
    out = execute_tool("project_state", {"action": "add_next", "text": "Ship it"},
                       {"workspace_path": ws, "project_id": "p-tool", "session_id": "s", "chat_mode": "WORKSPACE"})
    assert out == "Added to Next."
    assert _sections(ws, "p-tool")["Next"] == ["- [ ] Ship it"]
    assert execute_tool("project_state", {"action": "add_next", "text": "x"},
                        {"workspace_path": ws, "project_id": None, "session_id": "s"}).startswith("Error")


def test_remember_sends_decisions_and_tasks_to_the_file(ws):
    from app.agent.tools.registry import execute_tool

    ctx = {"workspace_path": ws, "project_id": "p-rem", "session_id": "s", "chat_mode": "WORKSPACE"}
    out = execute_tool("remember", {"content": "We use SQLite, not Postgres", "kind": "decision"}, ctx)
    assert out == "Saved to PROJECT.md: Added to Decisions."
    assert any("We use SQLite" in line for line in _sections(ws, "p-rem")["Decisions"])


@pytest.fixture
def api_db(tmp_path, monkeypatch):
    """The endpoints and the chat turn on one throwaway database (as in test_projects.py)."""
    from app.config import settings
    from app.database import get_session
    from app.main import app
    from app.memory.store import MemoryStore

    engine = create_engine(f"sqlite:///{tmp_path / 'api.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    (tmp_path / "workspace").mkdir()
    monkeypatch.setattr(settings, "workspace_path", str(tmp_path / "workspace"))

    def override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override
    monkeypatch.setattr("app.main.memory_store", MemoryStore(session_factory=lambda: Session(engine)))
    yield app
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_a_chat_reads_project_md_once_and_the_next_chat_sees_edits(ws, api_db):
    app = api_db

    heads: list[str] = []

    class Fake:
        async def chat(self, model=None, messages=None, tools=None):
            heads.append(messages[0]["content"])
            return {"message": {"role": "assistant", "content": "ok", "tool_calls": None}}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=10.0) as ac:
        r = await ac.post("/api/projects", json={"name": "Head test", "description": "Check the head",
                                                 "instructions": "Always say pineapple.", "workspace_path": ws})
        assert r.status_code == 201, r.text
        pid = r.json()["id"]
        r = await ac.get(f"/api/projects/{pid}/state")
        assert r.status_code == 200 and "## Decisions" in r.json()["content"]

        chat = {"message": "hi", "session_id": f"ps-{uuid.uuid4()}", "chat_mode": "WORKSPACE", "project_id": pid}
        with use_model_client(Fake()):
            await ac.post("/chat", json=chat)
            content = r.json()["content"].replace("## Now\n", "## Now\nWiring the head\n")
            r = await ac.put(f"/api/projects/{pid}/state", json={"content": content})
            assert r.status_code == 200
            await ac.post("/chat", json=chat)
            await ac.post("/chat", json={**chat, "session_id": f"ps-{uuid.uuid4()}"})

        too_big = await ac.put(f"/api/projects/{pid}/state", json={"content": "x" * (ps.STATE_MAX_CHARS + 1)})
        assert too_big.status_code == 400

    assert len(heads) >= 3
    first, second, new_chat = heads[0], heads[1], heads[-1]
    assert "Always say pineapple." in first and "--- Project State" in first
    assert first == second, "the head changed mid-chat"
    assert "Wiring the head" not in first and "Wiring the head" in new_chat
