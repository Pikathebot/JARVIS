"""
The two spaces (Freeform vs Workspace) and ephemeral / scratchpad turns.

Ephemeral: a turn runs normally, keeps its history in process memory, gets the persona and the
user's profile memories, and leaves nothing in the database -- no session, message, compaction
or tool-audit row, and so nothing in the session list. Deleting the session purges it at once.

Freeform: no project (not even the active one), no RAG, no workspace-bound tools -- offered or
executed -- and its sessions are listed apart from every workspace's.
"""
import json
import uuid
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from sqlmodel import func, select

from fake_providers import use_model_client
from app.agent.tools.registry import execute_tool, tools_for_space
from app.database import SessionLocal
from app.database.models import Attachment, CompactionEvent, Memory, Message, Project, ToolCallAudit
from app.database.models import Session as DBSession
from app.main import app
from app.memory.ephemeral import EphemeralRegistry, ephemeral_registry
from app.memory.store import DEFAULT_PROJECT_ID, MemoryStore


@pytest.fixture(autouse=True, scope="module")
def _schema():
    """The session-wide test database (see conftest) gets its tables from whoever needs them."""
    from sqlmodel import SQLModel
    from app.database.session import engine

    SQLModel.metadata.create_all(engine)


class RecordingModel:
    """Answers every call with a fixed reply and records what the model was sent."""

    def __init__(self, reply: str = "Noted."):
        self.reply = reply
        self.calls: list[dict] = []

    async def chat(self, model=None, messages=None, tools=None):
        self.calls.append({"messages": messages or [], "tools": tools or []})
        return {"message": {"role": "assistant", "content": self.reply, "tool_calls": None}}


def _row_counts() -> dict[str, int]:
    with SessionLocal() as db:
        return {
            model.__name__: db.exec(select(func.count()).select_from(model)).one()
            for model in (DBSession, Message, CompactionEvent, ToolCallAudit, Attachment)
        }


def _prompt_text(call: dict) -> str:
    return json.dumps(call["messages"], ensure_ascii=False)


def _tool_names(call: dict) -> set[str]:
    names = set()
    for tool in call["tools"]:
        if isinstance(tool, dict):
            names.add((tool.get("function") or {}).get("name") or tool.get("name"))
        else:
            names.add(getattr(tool, "__name__", str(tool)))
    return names


async def _post(path: str, **kwargs) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=20.0) as ac:
        return await ac.request(kwargs.pop("method", "POST"), path, **kwargs)


# ---- Ephemeral --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ephemeral_turns_keep_history_in_memory_and_write_nothing():
    session_id = f"eph-{uuid.uuid4().hex}"
    fake = RecordingModel()
    before = _row_counts()

    with use_model_client(fake):
        first = await _post("/chat", json={"message": "My cat is called Biscuit.", "session_id": session_id, "ephemeral": True})
        second = await _post("/chat", json={"message": "What is my cat called?", "session_id": session_id, "ephemeral": True})

    assert first.status_code == 200 and second.status_code == 200
    # The second turn saw the first one: the history is real, just not stored.
    assert "Biscuit" in _prompt_text(fake.calls[-1])
    assert _row_counts() == before
    listed = (await _post("/sessions", method="GET", params={"limit": 0})).json()
    assert session_id not in {s["session_id"] for s in listed}

    purged = await _post(f"/sessions/{session_id}", method="DELETE")
    assert purged.status_code == 200 and purged.json()["ephemeral"] is True
    assert not ephemeral_registry.contains(session_id)


@pytest.mark.asyncio
async def test_ephemeral_stream_writes_nothing_and_purge_drops_the_context():
    session_id = f"eph-{uuid.uuid4().hex}"
    fake = RecordingModel("Streaming reply.")
    before = _row_counts()

    with use_model_client(fake):
        res = await _post("/chat/stream", json={"message": "Remember the code word: heliotrope.", "session_id": session_id, "ephemeral": True})
        assert res.status_code == 200 and "event: done" in res.text
        await _post(f"/sessions/{session_id}", method="DELETE")
        await _post("/chat/stream", json={"message": "What was the code word?", "session_id": session_id, "ephemeral": True})

    assert _row_counts() == before
    # Purged between the turns: the second one starts from nothing.
    assert "heliotrope" not in _prompt_text(fake.calls[-1])
    ephemeral_registry.purge(session_id)


@pytest.mark.asyncio
async def test_every_turn_carries_profile_memories_and_only_ephemeral_says_off_the_record():
    fact = f"Prefers metric units ({uuid.uuid4().hex[:6]})"
    with SessionLocal() as db:
        db.add(Memory(category="user_preference", content=fact, project_id=None, pinned=True))
        db.add(Memory(category="important_decision", content="Chose Postgres for project X", project_id=None))
        db.commit()

    fake = RecordingModel()
    eph_id = f"eph-{uuid.uuid4().hex}"
    with use_model_client(fake):
        await _post("/chat", json={"message": "How far is 10 miles?", "session_id": eph_id, "ephemeral": True})
        ephemeral_prompt = _prompt_text(fake.calls[-1])
        await _post("/chat", json={"message": "How far is 10 miles?", "session_id": f"s-{uuid.uuid4().hex}"})
        ordinary_prompt = _prompt_text(fake.calls[-1])

    assert fact in ephemeral_prompt
    assert "Chose Postgres" not in ephemeral_prompt  # not a profile category
    assert "off-the-record" in ephemeral_prompt
    system = fake.calls[-1]["messages"][0]
    assert system["role"] == "system" and system["content"]  # persona preamble still heads the prompt
    assert fact in ordinary_prompt
    assert "off-the-record" not in ordinary_prompt
    assert fact not in system["content"]  # per-turn, never in the cached prefix
    ephemeral_registry.purge(eph_id)


def test_ephemeral_store_refuses_unvetted_store_methods():
    from app.memory.ephemeral import EphemeralMemoryStore

    store = EphemeralMemoryStore(MemoryStore(session_factory=SessionLocal), registry=EphemeralRegistry())
    with pytest.raises(AttributeError):
        store.calculate_rolling_reliability()


def test_a_reply_finishing_after_the_purge_does_not_resurrect_the_session():
    from app.memory.ephemeral import EphemeralMemoryStore

    registry = EphemeralRegistry()
    store = EphemeralMemoryStore(MemoryStore(session_factory=SessionLocal), registry=registry)
    store.get_or_create_session("eph-late", chat_mode="FREEFORM")
    store.append_message("eph-late", role="user", content="hello")
    registry.purge("eph-late")
    store.append_message("eph-late", role="assistant", content="late reply")
    store.record_compaction("eph-late", "summary", 10, 5)
    assert not registry.contains("eph-late")


def test_idle_ephemeral_sessions_expire():
    registry = EphemeralRegistry(idle_ttl_seconds=0)
    registry.get_or_create("old", "FREEFORM", None)
    registry.get_or_create("new", "FREEFORM", None)  # the access that notices "old" went idle
    assert not registry.contains("old")


@pytest.mark.asyncio
async def test_ephemeral_upload_writes_no_rows_and_is_deleted_with_the_session():
    session_id = f"eph-{uuid.uuid4().hex}"
    before = _row_counts()
    res = await _post(
        "/api/upload",
        data={"session_id": session_id, "ephemeral": "true"},
        files={"file": ("scratch.txt", b"throwaway notes", "text/plain")},
    )
    assert res.status_code == 201, res.text
    path = Path(res.json()["path"])
    assert path.read_bytes() == b"throwaway notes"
    assert _row_counts() == before

    await _post(f"/sessions/{session_id}", method="DELETE")
    assert not path.exists()


@pytest.mark.asyncio
async def test_ephemeral_upload_rejects_a_path_like_session_id():
    res = await _post(
        "/api/upload",
        data={"session_id": "..\\..\\evil", "ephemeral": "true"},
        files={"file": ("x.txt", b"x", "text/plain")},
    )
    assert res.status_code == 400


# ---- Freeform -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_freeform_turn_has_no_project_rag_or_workspace_tools():
    project_id = f"proj-{uuid.uuid4().hex[:8]}"
    with SessionLocal() as db:
        for active in db.exec(select(Project).where(Project.is_active == True)).all():  # noqa: E712
            active.is_active = False
            db.add(active)
        db.add(Project(id=project_id, name="Active one", is_active=True))
        db.commit()

    session_id = f"ff-{uuid.uuid4().hex}"
    fake = RecordingModel()
    try:
        with use_model_client(fake), patch("app.rag.retriever.HybridRetriever.retrieve") as retrieve:
            res = await _post("/chat", json={
                "message": "Read the file notes.txt and fix the code in it",
                "session_id": session_id,
                "chat_mode": "FREEFORM",
            })
        assert res.status_code == 200
        retrieve.assert_not_called()
        assert "SPACE: Freeform" in _prompt_text(fake.calls[-1])
        assert "SPACE: Freeform" not in fake.calls[-1]["messages"][0]["content"]  # per-turn, not the cached prefix
        offered = _tool_names(fake.calls[-1])
        assert offered.isdisjoint({"read_file", "write_file", "patch_file", "list_directory", "execute_command"})

        with SessionLocal() as db:
            row = db.get(DBSession, session_id)
            assert row.chat_mode == "FREEFORM" and row.project_id is None

        freeform = (await _post("/sessions", method="GET", params={"chat_mode": "FREEFORM", "limit": 0})).json()
        workspace = (await _post("/sessions", method="GET", params={"project_id": DEFAULT_PROJECT_ID, "limit": 0})).json()
        assert session_id in {s["session_id"] for s in freeform}
        assert session_id not in {s["session_id"] for s in workspace}
    finally:
        with SessionLocal() as db:
            proj = db.get(Project, project_id)
            if proj:
                proj.is_active = False
                db.add(proj)
                db.commit()


def test_freeform_offers_no_workspace_tools():
    names = {t.__name__ for t in tools_for_space("FREEFORM")}
    assert "read_file" not in names and "git_status" not in names
    assert "web_search" in names  # non-workspace tools are still there


def test_execute_tool_refuses_workspace_tools_in_freeform():
    out = execute_tool("read_file", {"path": "notes.txt"}, context={"chat_mode": "FREEFORM", "workspace_path": "."})
    assert out.startswith("Error:") and "Freeform" in out


def test_session_lists_keep_the_spaces_apart(tmp_path):
    store = MemoryStore(db_path=str(tmp_path / "spaces.db"))
    store.get_or_create_session("ws-unscoped", chat_mode="WORKSPACE", project_id=None)
    store.get_or_create_session("ff-1", chat_mode="FREEFORM", project_id=None)

    default_ws = {s["session_id"] for s in store.list_sessions(project_id=DEFAULT_PROJECT_ID)}
    everything_but_freeform = {s["session_id"] for s in store.list_sessions()}
    freeform = {s["session_id"] for s in store.list_sessions(chat_mode="FREEFORM")}

    assert default_ws == {"ws-unscoped"}
    assert everything_but_freeform == {"ws-unscoped"}
    assert freeform == {"ff-1"}
