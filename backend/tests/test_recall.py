"""``recall`` (PLAN 4.18 phase 3, app/agent/tools/recall.py): keyword search over messages (FTS5),
keyword + meaning over thread episode records, past time spans, and the scope rule -- Freeform
sees the thread and every real project's chats, a project chat only its own project."""
import json
import time
from datetime import datetime

import pytest
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine

from app.agent.tools import recall as recall_mod
from app.agent.tools.recall import parse_past, recall
from app.agent.tools.registry import tools_for_space
from app.database.models import Project
from app.memory.store import MemoryStore
from app.memory.thread import THREAD_SESSION_ID, ThreadEpisodes

NOW = datetime(2026, 10, 20, 10, 0)  # a Tuesday


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_BRIEFING_STATE", str(tmp_path / "briefing.json"))
    engine = create_engine(f"sqlite:///{tmp_path / 'recall.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    factory = lambda: Session(engine)  # noqa: E731
    monkeypatch.setattr(recall_mod, "session_factory", factory)
    monkeypatch.setattr(recall_mod, "_sidecar_vectors", lambda texts, query: None)
    store = MemoryStore(session_factory=factory)
    with factory() as db:
        db.add(Project(id="p-thesis", name="Thesis"))
        db.add(Project(id="p-game", name="Game"))
        db.add(Project(id="default-workspace", name="Default"))
        db.commit()
    return store, engine


def _chat(store, sid, user, reply, project=None):
    store.get_or_create_session(sid, chat_mode="WORKSPACE" if project else "FREEFORM", project_id=project)
    store.append_message(sid, role="user", content=user)
    store.append_message(sid, role="assistant", content=reply)


def test_past_spans():
    day = lambda ts: datetime.fromtimestamp(ts).date().isoformat()  # noqa: E731
    a, b = parse_past("yesterday", NOW)
    assert day(a) == "2026-10-19" and b - a == 86400
    assert day(parse_past("monday", NOW)[0]) == "2026-10-19"
    assert day(parse_past("last tuesday", NOW)[0]) == "2026-10-13"
    assert day(parse_past("tuesday", NOW)[0]) == "2026-10-20"
    a, b = parse_past("last week", NOW)
    assert day(a) == "2026-10-12" and day(b) == "2026-10-19"
    assert day(parse_past("3 days ago", NOW)[0]) == "2026-10-17"
    assert day(parse_past("18 oct", NOW)[0]) == "2026-10-18"
    assert day(parse_past("dec 25", NOW)[0]) == "2025-12-25", "a date ahead means last year"
    assert day(parse_past("2026-10-01", NOW)[0]) == "2026-10-01"
    assert day(parse_past("Sunday 18 Oct", NOW)[0]) == "2026-10-18", "Ornith's own form, live 2026-10-20"
    assert day(parse_past("18th october 2025", NOW)[0]) == "2025-10-18"
    assert day(parse_past("sun", NOW)[0]) == "2026-10-18"
    assert parse_past("someday soon", NOW) is None


def test_keyword_hits_come_with_the_episode_record_and_skip_the_open_episode(env):
    store, _ = env
    eps = ThreadEpisodes(store)
    first = eps.context(THREAD_SESSION_ID)
    _chat(store, THREAD_SESSION_ID, "the GPU hit 83 degrees in Elden Ring", "That's within spec, sir.")
    eps.context(THREAD_SESSION_ID, new_topic=True)
    store.update_episode(first.episode["id"], record_status="done", title="GPU temperatures",
                         summary="The user saw 83 C in Elden Ring.", decisions="keep the fan curve")
    _chat(store, THREAD_SESSION_ID, "GPU again: is 85 fine?", "Yes.")  # the open episode: in context already

    out = recall(query="GPU temperature", session_id=THREAD_SESSION_ID)
    assert "GPU temperatures" in out and "Summary: The user saw 83 C" in out and "Decided: keep the fan curve" in out
    assert "83 degrees in Elden Ring" in out
    assert "is 85 fine" not in out


def test_scope_freeform_sees_projects_but_a_project_sees_only_itself(env):
    store, _ = env
    _chat(store, "s-thesis", "the citation style is APA seven", "Noted.", project="p-thesis")
    _chat(store, "s-game", "the citation style for the game wiki is MLA", "Ok.", project="p-game")
    _chat(store, "s-default", "citation style test", "x", project="default-workspace")
    _chat(store, "s-old-freeform", "citation style in an old freeform chat", "x")

    everywhere = recall(query="citation style", session_id=THREAD_SESSION_ID)
    assert "Project chat in Thesis" in everywhere and "Project chat in Game" in everywhere
    assert "test" not in everywhere and "old freeform" not in everywhere

    in_thesis = recall(query="citation style", session_id="s-thesis", project_id="p-thesis")
    assert "APA seven" in in_thesis and "MLA" not in in_thesis


def test_a_time_span_alone_lists_what_was_discussed(env):
    store, engine = env
    eps = ThreadEpisodes(store)
    first = eps.context(THREAD_SESSION_ID)
    _chat(store, THREAD_SESSION_ID, "plan the Ooty trip", "Two days.")
    eps.context(THREAD_SESSION_ID, new_topic=True)
    yesterday = time.time() - 86400
    store.update_episode(first.episode["id"], record_status="done", title="Ooty trip", started_at=yesterday,
                         ended_at=yesterday + 600)
    out = recall(when="yesterday", session_id=THREAD_SESSION_ID)
    assert "Ooty trip" in out
    assert recall(when="last month", session_id=THREAD_SESSION_ID).startswith("No conversations found")


def test_meaning_matches_a_record_without_shared_words(env, monkeypatch):
    store, _ = env
    eps = ThreadEpisodes(store)
    first = eps.context(THREAD_SESSION_ID)
    _chat(store, THREAD_SESSION_ID, "my laptop fans scream when gaming", "Use Game mode.")
    eps.context(THREAD_SESSION_ID, new_topic=True)
    store.update_episode(first.episode["id"], record_status="done", title="Loud fans while gaming",
                         embedding=json.dumps([1.0, 0.0, 0.0]))
    monkeypatch.setattr(recall_mod, "_sidecar_vectors", lambda texts, query: [[0.9, 0.1, 0.0]])
    out = recall(query="noise", session_id=THREAD_SESSION_ID)
    assert "Loud fans while gaming" in out


def test_the_index_follows_deletes(env):
    store, _ = env
    _chat(store, "s-thesis", "the secret phrase is bluebird", "Ok.", project="p-thesis")
    assert "bluebird" in recall(query="bluebird", session_id=THREAD_SESSION_ID)
    store.delete_session("s-thesis")
    assert recall(query="bluebird", session_id=THREAD_SESSION_ID).startswith("Nothing found")


def test_errors_and_where_it_is_offered(env):
    assert recall(session_id=THREAD_SESSION_ID).startswith("Error: say what to look for")
    assert recall(query="x", when="someday", session_id=THREAD_SESSION_ID).startswith("Error: couldn't read")
    names = lambda space: [f.__name__ for f in tools_for_space(space)]  # noqa: E731
    assert "recall" in names("FREEFORM") and "recall" in names("WORKSPACE")


def test_a_model_cannot_widen_the_scope_through_its_arguments(env):
    from app.agent.tools.registry import execute_tool
    store, _ = env
    _chat(store, "s-thesis", "the citation style is APA seven", "Noted.", project="p-thesis")
    _chat(store, "s-game", "the citation style for the wiki is MLA", "Ok.", project="p-game")
    out = execute_tool("recall", {"query": "citation style", "project_id": "", "session_id": THREAD_SESSION_ID},
                       {"project_id": "p-thesis", "session_id": "s-thesis"})
    assert "APA seven" in out and "MLA" not in out
