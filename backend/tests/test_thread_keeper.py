"""The thread keeper (PLAN 4.18 phase 2, app/memory/thread_keeper.py): closes quiet episodes,
writes their records with the main model only when it is loaded and the thread is quiet, folds
ahead with a real summary, refreshes a digest that was built while records were pending, and the
first episode of the day carries the morning briefing."""
import time
from datetime import datetime

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.awareness import day_briefing
from app.memory.store import MemoryStore
from app.memory.thread import THREAD_SESSION_ID, ThreadEpisodes
from app.memory.thread_keeper import QUIET_SECONDS, ThreadKeeper, transcript

HOURS = 3600.0


class FakeProvider:
    def __init__(self, titles=None, fail=False):
        self.titles = list(titles or [])
        self.fail = fail
        self.calls = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("llama-server down")
        title = self.titles.pop(0) if self.titles else f"Topic {len(self.calls)}"
        args = {"title": title, "summary": f"The user talked about {title.lower()}.",
                "decisions": "use Ornith", "open_items": "book the hotel"}
        return {"message": {"tool_calls": [{"function": {"name": "episode_record", "arguments": args}}]}}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_BRIEFING_STATE", str(tmp_path / "briefing.json"))
    engine = create_engine(f"sqlite:///{tmp_path / 'keeper.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return MemoryStore(session_factory=lambda: Session(engine))


def _say(store, user, reply):
    store.append_message(THREAD_SESSION_ID, role="user", content=user)
    store.append_message(THREAD_SESSION_ID, role="assistant", content=reply)


def _keeper(store, provider, usable=True, **kw):
    state = {"usable": usable}
    keeper = ThreadKeeper(store, lambda: provider, lambda: state["usable"], **kw)
    return keeper, state


@pytest.mark.asyncio
async def test_a_quiet_episode_is_closed_and_recorded_only_when_main_is_usable(store):
    ThreadEpisodes(store).context(THREAD_SESSION_ID)
    _say(store, "my gpu runs hot", "It peaked at 83 C.")
    provider = FakeProvider(["GPU temperatures"])
    keeper, state = _keeper(store, provider, usable=False)

    did = await keeper.tick(now=time.time() + 4 * HOURS)
    assert did == [f"closed {store.list_episodes(THREAD_SESSION_ID)[0]['id']}"]
    assert store.list_episodes(THREAD_SESSION_ID)[0]["record_status"] == "pending"
    assert provider.calls == [], "main not loaded: the record waits"

    state["usable"] = True
    did = await keeper.tick(now=time.time() + 4 * HOURS)
    ep = store.list_episodes(THREAD_SESSION_ID)[0]
    assert ep["record_status"] == "done" and ep["title"] == "GPU temperatures"
    assert ep["open_items"] == "book the hotel" and did[-1].startswith("recorded")
    sent = provider.calls[0]
    assert sent["tool_choice"] == "required" and sent["thinking"] is False
    assert "my gpu runs hot" in sent["messages"][-1]["content"]

    # The next episode's digest names it by its record.
    later = ThreadEpisodes(store).context(THREAD_SESSION_ID, now=time.time() + 5 * HOURS)
    assert "GPU temperatures" in later.preamble and "book the hotel" in later.preamble


@pytest.mark.asyncio
async def test_no_model_work_while_the_thread_is_active_or_the_call_fails(store):
    eps = ThreadEpisodes(store)
    eps.context(THREAD_SESSION_ID)
    _say(store, "first", "one")
    eps.context(THREAD_SESSION_ID, new_topic=True)  # closes the first: pending
    _say(store, "second", "two")
    provider = FakeProvider()
    keeper, _ = _keeper(store, provider)
    assert await keeper.tick() == [], "a message less than a minute old: wait"
    assert provider.calls == []

    failing = FakeProvider(fail=True)
    keeper, _ = _keeper(store, failing)
    await keeper.tick(now=time.time() + QUIET_SECONDS + 5)
    assert [e["record_status"] for e in store.list_episodes(THREAD_SESSION_ID)] == ["open", "pending"]


@pytest.mark.asyncio
async def test_a_digest_built_while_pending_is_refreshed_once_the_record_exists(store):
    eps = ThreadEpisodes(store)
    eps.context(THREAD_SESSION_ID)
    _say(store, "plan the trip to Ooty", "Two days, sir.")
    opened = eps.context(THREAD_SESSION_ID, new_topic=True).episode
    assert "summary pending" in opened["digest"]

    keeper, _ = _keeper(store, FakeProvider(["Ooty trip"]))
    did = await keeper.tick(now=time.time() + QUIET_SECONDS + 5)
    assert f"digest {opened['id']}" in did
    fresh = store.open_episode(THREAD_SESSION_ID)["digest"]
    assert "Ooty trip" in fresh and "summary pending" not in fresh


@pytest.mark.asyncio
async def test_the_keeper_no_longer_folds_ahead(store):
    # Folding ahead moved to app.memory.compaction (PLAN 4.25): after a turn past 80% of the
    # window, with a Claude Code-style summary. The keeper only writes records and digests now.
    eps = ThreadEpisodes(store, budget_tokens=100_000)
    eps.context(THREAD_SESSION_ID)
    for i in range(10):
        _say(store, f"question {i} " + "word " * 15, f"answer {i} " + "word " * 15)
    keeper, _ = _keeper(store, FakeProvider(["Long chat"]), episodes=eps)
    did = await keeper.tick(now=time.time() + QUIET_SECONDS + 5)
    assert not any(d.startswith("folded") for d in did)
    assert not store.open_episode(THREAD_SESSION_ID).get("fold_note")


def test_the_first_episode_of_the_day_carries_the_briefing(store):
    today = day_briefing.briefing_day(datetime.now())
    day_briefing.mark_briefed(today, "Good morning, sir.\nStand-up at 10 AM.\n2 tasks due today.")
    eps = ThreadEpisodes(store)
    first = eps.context(THREAD_SESSION_ID)
    assert "This morning's briefing: Good morning, sir. Stand-up at 10 AM. 2 tasks due today." in first.preamble
    _say(store, "hi", "Hello.")
    second = eps.context(THREAD_SESSION_ID, new_topic=True)
    assert "briefing" not in second.preamble, "only the day's first episode"
    assert day_briefing.briefing_text(today) and not day_briefing.briefing_text(today.replace(year=2000))


def test_transcript_clips_tool_output_and_keeps_the_words():
    rows = [
        {"role": "user", "content": "read the log", "created_at": time.time()},
        {"role": "tool", "name": "read_file", "content": "x" * 2000},
        {"role": "assistant", "content": "The log shows 3 errors."},
    ]
    text = transcript(rows, prior_note="earlier: set up the eval")
    assert text.startswith("(Before this part: earlier: set up the eval)")
    assert "User " in text and "Jarvis: The log shows 3 errors." in text
    assert "[tool read_file: " + "x" * 400 + " ...]" in text


def test_placeholder_fields_are_empty_and_the_fold_note_reads_cleanly():
    from app.memory.thread_keeper import _field, fold_note_from
    assert _field("None noted.") == "" and _field("n/a") == "" and _field("  book  the hotel ") == "book the hotel"
    note = fold_note_from({"summary": "The user set up Steam", "decisions": "use the force close.", "open_items": ""})
    assert note == "[Earlier in this episode (folded): The user set up Steam. Decided: use the force close.]"


def test_the_digest_clips_long_record_fields(store):
    eps = ThreadEpisodes(store)
    first = eps.context(THREAD_SESSION_ID)
    _say(store, "a", "b")
    store.update_episode(first.episode["id"], title="Long one", summary="word " * 400,
                         decisions="choice " * 200, record_status="done")
    eps.store.update_episode(first.episode["id"], ended_at=time.time())
    digest = eps.context(THREAD_SESSION_ID, new_topic=True).preamble
    assert len(digest) < 1400 and digest.count("...") >= 2
