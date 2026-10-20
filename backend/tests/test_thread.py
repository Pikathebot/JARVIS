"""The one continuing Freeform thread (PLAN 4.18, app/memory/thread.py): episodes open on a 3-hour
gap or "new topic", the model sees only the open episode, its history is append-only (prompt
prefix stable turn to turn) and an over-long episode folds once."""
import time

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.agent.orchestrator import AgentOrchestrator, _NEW_TOPIC_REPLY
from app.memory.context_manager import ContextManager
from app.memory.store import MemoryStore
from app.memory.thread import THREAD_SESSION_ID, ThreadEpisodes, is_thread, split_new_topic

HOURS = 3600.0


@pytest.fixture
def store(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'thread.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return MemoryStore(session_factory=lambda: Session(engine))


def _say(store, user, reply):
    store.append_message(THREAD_SESSION_ID, role="user", content=user)
    store.append_message(THREAD_SESSION_ID, role="assistant", content=reply)


def _build(store, ctx, message):
    return ContextManager(memory_store=store).build_context(
        session_id=THREAD_SESSION_ID, user_message=message, system_prompt="You are Jarvis.",
        chat_mode="FREEFORM", max_context_tokens=32768,
        history_rows=ctx.rows, history_preamble=ctx.preamble)


def test_new_topic_phrases():
    assert split_new_topic("new topic") == (True, "")
    assert split_new_topic("Jarvis, new topic.") == (True, "")
    assert split_new_topic("Let's start a new topic: what's the weather?") == (True, "what's the weather?")
    assert split_new_topic("New topic - book a table") == (True, "book a table")
    assert split_new_topic("is this a new topic?") == (False, "is this a new topic?")
    assert split_new_topic("topics for my essay") == (False, "topics for my essay")
    assert is_thread(THREAD_SESSION_ID) and not is_thread("session_abc")


def test_a_gap_opens_a_new_episode_and_the_old_one_waits_for_its_record(store):
    eps = ThreadEpisodes(store)
    first = eps.context(THREAD_SESSION_ID)
    assert first.opened and first.preamble == "" and first.rows == []
    _say(store, "what's my vram?", "5.1 GB of 8.")

    same = eps.context(THREAD_SESSION_ID, now=time.time() + 2 * HOURS)
    assert not same.opened and same.episode["id"] == first.episode["id"]
    assert [r["content"] for r in same.rows] == ["what's my vram?", "5.1 GB of 8."]

    later = eps.context(THREAD_SESSION_ID, now=time.time() + 4 * HOURS)
    assert later.opened and later.episode["id"] != first.episode["id"]
    assert later.rows == [], "the model sees nothing from the closed episode"
    closed = [e for e in store.list_episodes(THREAD_SESSION_ID) if e["id"] == first.episode["id"]][0]
    assert closed["record_status"] == "pending" and closed["ended_at"] is not None
    # The digest names the earlier episode (by time until phase 2 writes its record).
    assert "Earlier in this conversation" in later.preamble and "summary pending" in later.preamble


def test_new_topic_cuts_early_and_the_digest_uses_records(store):
    eps = ThreadEpisodes(store)
    first = eps.context(THREAD_SESSION_ID)
    _say(store, "plan my trip", "Sure.")
    store.update_episode(first.episode["id"], title="Trip planning")
    cut = eps.context(THREAD_SESSION_ID, new_topic=True)
    assert cut.opened and cut.episode["id"] != first.episode["id"]
    # The closed episode's record was written after it closed (phase 2) -- the *next* digest uses it.
    store.update_episode(first.episode["id"], summary="Two days in Ooty.", open_items="book the hotel",
                         record_status="done")
    third = eps.context(THREAD_SESSION_ID, new_topic=True)
    assert "Trip planning" in third.preamble
    assert "Two days in Ooty." not in third.preamble, "summary is the *previous* episode's only"
    assert "book the hotel" in third.preamble


def test_episode_history_is_append_only_so_the_prefix_stays_cached(store):
    eps = ThreadEpisodes(store)
    eps.context(THREAD_SESSION_ID)
    _say(store, "old question", "old answer")
    eps.context(THREAD_SESSION_ID, now=time.time() + 4 * HOURS)  # new episode, with a digest

    # Turn 1 of the episode: no history yet, the digest rides on the current message after the words.
    ctx = eps.context(THREAD_SESSION_ID)
    pkg1 = _build(store, ctx, "first in episode")
    assert pkg1.messages[-1]["content"].startswith("first in episode")
    assert "Earlier in this conversation" in pkg1.messages[-1]["content"]
    assert all("old question" not in str(m["content"]) for m in pkg1.messages)
    _say(store, "first in episode", "reply one")

    # Turns 2 and 3: the digest now rides on the stored first message, byte-identical each time.
    pkg2 = _build(store, eps.context(THREAD_SESSION_ID), "second")
    _say(store, "second", "reply two")
    pkg3 = _build(store, eps.context(THREAD_SESSION_ID), "third")
    assert pkg2.messages[1]["content"].startswith("first in episode\n\n[Earlier in this conversation")
    assert pkg3.messages[:3] == pkg2.messages[:3], "history prefix unchanged turn to turn"
    assert [i for i, m in enumerate(pkg3.messages) if m["role"] == "system"] == [0]


def test_more_than_twenty_messages_are_all_kept_within_an_episode(store):
    eps = ThreadEpisodes(store)
    eps.context(THREAD_SESSION_ID)
    for i in range(15):
        _say(store, f"q{i}", f"a{i}")
    pkg = _build(store, eps.context(THREAD_SESSION_ID), "next")
    assert pkg.messages[1]["content"] == "q0", "no sliding window inside an episode"


def test_an_over_long_episode_folds_once_then_appends_again(store):
    eps = ThreadEpisodes(store, budget_tokens=400)
    eps.context(THREAD_SESSION_ID)
    for i in range(12):
        _say(store, f"question {i} " + "word " * 20, f"answer {i} " + "word " * 20)
    folded = eps.context(THREAD_SESSION_ID)
    assert folded.folded_now and folded.rows[0]["role"] == "user"
    assert "folded away" in folded.preamble
    kept_from = folded.rows[0]["id"]

    _say(store, "one more", "ok")
    after = eps.context(THREAD_SESSION_ID)
    assert not after.folded_now, "the fold point is stored: no re-fold every turn"
    assert after.rows[0]["id"] == kept_from and after.preamble == folded.preamble


@pytest.mark.asyncio
async def test_new_topic_alone_is_answered_in_code_and_stores_nothing(store):
    orch = AgentOrchestrator(memory_store=store)
    store.append_message(THREAD_SESSION_ID, role="user", content="earlier")
    before = store.last_message_meta(THREAD_SESSION_ID)["id"]
    events = [ev async for ev in orch.run_stream("Jarvis, new topic", session_id=THREAD_SESSION_ID,
                                                chat_mode="FREEFORM")]
    kinds = [e["event"] for e in events]
    assert kinds == ["episode", "token", "done"]
    assert events[-1]["data"]["response"] == _NEW_TOPIC_REPLY
    assert store.last_message_meta(THREAD_SESSION_ID)["id"] == before
    opened = store.open_episode(THREAD_SESSION_ID)
    assert opened is not None and opened["start_after_message_id"] == before
