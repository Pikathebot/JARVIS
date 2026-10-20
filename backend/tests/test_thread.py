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
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_BRIEFING_STATE", str(tmp_path / "briefing.json"))  # no briefing today
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


def test_message_pages_mark_where_each_episode_starts(store, monkeypatch):
    import app.main as main
    from app.routers import thread as thread_router
    monkeypatch.setattr(main, "memory_store", store)
    eps = ThreadEpisodes(store)
    first = eps.context(THREAD_SESSION_ID)
    _say(store, "one", "1")
    second = eps.context(THREAD_SESSION_ID, new_topic=True)
    _say(store, "two", "2")
    _say(store, "three", "3")
    page = thread_router.list_messages(before_id=None, limit=80)
    starts = [(m["content"], m.get("episode_start")) for m in page["messages"]]
    assert starts[0] == ("one", first.episode["id"]) and starts[2] == ("two", second.episode["id"])
    assert all(s is None for c, s in starts if c not in ("one", "two"))
    # A page that begins exactly where an episode began still says so.
    newest = thread_router.list_messages(before_id=None, limit=4)
    assert newest["has_more"] and newest["messages"][0]["content"] == "two"
    assert newest["messages"][0]["episode_start"] == second.episode["id"]
    older = thread_router.list_messages(before_id=newest["messages"][0]["id"], limit=10)
    assert [m["content"] for m in older["messages"]] == ["one", "1"] and not older["has_more"]
    # The side-chat list leaves the thread out.
    store.get_or_create_session("side", chat_mode="FREEFORM")
    store.append_message("side", role="user", content="side chat")
    assert [s["session_id"] for s in store.list_sessions(chat_mode="FREEFORM")] == ["side"]


@pytest.mark.asyncio
async def test_a_non_streaming_turn_in_the_thread_goes_through_its_episodes(store):
    """The HUD's POST /chat turns land in the thread too: run() takes the streaming path."""
    orch = AgentOrchestrator(memory_store=store)
    result = await orch.run("new topic", session_id=THREAD_SESSION_ID, chat_mode="FREEFORM")
    assert result.response == _NEW_TOPIC_REPLY and result.status == "completed"
    assert result.session_id == THREAD_SESSION_ID
    assert store.open_episode(THREAD_SESSION_ID) is not None


# --- phase 5: forgetting ------------------------------------------------------------------------

@pytest.fixture
def forget_db(tmp_path, monkeypatch):
    """A store and the factory the forget tool reads through, on one database."""
    from app.agent.tools import forget_conversation as fc
    monkeypatch.setenv("JARVIS_BRIEFING_STATE", str(tmp_path / "briefing.json"))
    engine = create_engine(f"sqlite:///{tmp_path / 'forget.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    factory = lambda: Session(engine)  # noqa: E731
    monkeypatch.setattr(fc, "session_factory", factory)
    return MemoryStore(session_factory=factory)


def _three_topics(store):
    """Trip (closed, titled), GPU (closed, titled), then an open one."""
    eps = ThreadEpisodes(store)
    trip = eps.context(THREAD_SESSION_ID).episode
    _say(store, "plan the Ooty trip", "Two days in Ooty.")
    gpu = eps.context(THREAD_SESSION_ID, new_topic=True).episode
    _say(store, "why is the gpu hot", "Game mode was off.")
    store.update_episode(trip["id"], title="Trip to Ooty", summary="Two days in Ooty.", record_status="done")
    store.update_episode(gpu["id"], title="GPU temperature", record_status="done")
    now = eps.context(THREAD_SESSION_ID, new_topic=True).episode
    _say(store, "hello again", "Hi.")
    return trip, gpu, now


def test_forgetting_an_episode_deletes_its_messages_and_scrubs_later_digests(forget_db):
    from app.memory.thread import forget_episodes
    store = forget_db
    trip, gpu, now = _three_topics(store)
    gpu_digest = [e for e in store.list_episodes(THREAD_SESSION_ID) if e["id"] == gpu["id"]][0]["digest"]
    assert gpu_digest and gpu_digest.count("\n- ") == 1  # it listed the trip (untitled when it opened)

    result = forget_episodes(store, [trip["id"]])
    assert result == {"episodes": 1, "messages": 2}
    contents = [m["content"] for m in store.get_messages(THREAD_SESSION_ID)]
    assert "plan the Ooty trip" not in contents and "why is the gpu hot" in contents and "hello again" in contents
    left = {e["id"]: e for e in store.list_episodes(THREAD_SESSION_ID)}
    assert trip["id"] not in left
    assert not left[gpu["id"]]["digest"], "the GPU topic's digest named only the trip: now empty"
    assert "Ooty" not in (left[now["id"]]["digest"] or "") and "GPU temperature" in left[now["id"]]["digest"]


def test_delete_topic_endpoint(forget_db, monkeypatch):
    import app.main as main
    from fastapi import HTTPException
    from app.routers import thread as thread_router
    monkeypatch.setattr(main, "memory_store", forget_db)
    trip, gpu, now = _three_topics(forget_db)
    out = thread_router.delete_episode(gpu["id"])
    assert out["deleted"] and out["messages"] == 2
    with pytest.raises(HTTPException):
        thread_router.delete_episode(gpu["id"])


def test_forget_conversation_tool_finds_by_topic_and_only_when_asked(forget_db):
    from app.agent.tools.forget_conversation import describe_matches, forget_conversation
    store = forget_db
    trip, gpu, now = _three_topics(store)

    count, card = describe_matches("the ooty trip")
    assert count == 1 and "Trip to Ooty" in card and "GPU" not in card

    unasked = forget_conversation("ooty trip", session_id=THREAD_SESSION_ID, user_message="tell me about ooty")
    assert unasked.startswith("Error") and len(store.list_episodes(THREAD_SESSION_ID)) == 3
    elsewhere = forget_conversation("ooty trip", session_id="side", user_message="forget the ooty trip")
    assert elsewhere.startswith("Error")

    done = forget_conversation("ooty trip", session_id=THREAD_SESSION_ID,
                               user_message="forget what we said about the ooty trip")
    assert "Trip to Ooty" in done
    assert [e["id"] for e in store.list_episodes(THREAD_SESSION_ID)] == [now["id"], gpu["id"]]
    # The open topic is never a match, whatever it says.
    assert describe_matches("hello again")[0] == 0


def test_forget_conversation_asks_with_a_card_naming_what_goes(forget_db):
    from app.agent.permissions import RiskTier, evaluate_tool_permission
    _three_topics(forget_db)
    asked = evaluate_tool_permission("forget_conversation", {"about": "gpu"},
                                     user_message="forget the gpu stuff", chat_mode="FREEFORM")
    assert asked.risk_tier == RiskTier.CONFIRMATION_REQUIRED and "GPU temperature" in asked.reason
    nothing = evaluate_tool_permission("forget_conversation", {"about": "bananas"},
                                       user_message="forget bananas", chat_mode="FREEFORM")
    assert nothing.risk_tier == RiskTier.LOW_RISK


def test_forget_conversation_is_offered_in_freeform_only():
    from app.agent.tools.registry import tools_for_space
    assert "forget_conversation" in [t.__name__ for t in tools_for_space("FREEFORM")]
    assert "forget_conversation" not in [t.__name__ for t in tools_for_space("WORKSPACE")]


# --- topic names from the model -----------------------------------------------------------------

class _Namer:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    async def chat(self, messages, **kw):
        self.calls.append((messages, kw))
        return {"message": {"content": self.reply}}


@pytest.mark.asyncio
async def test_the_open_topic_is_named_after_its_first_reply_and_renamed_every_few_turns(store):
    from app.memory import thread_titles as tt
    eps = ThreadEpisodes(store)
    ep = eps.context(THREAD_SESSION_ID).episode
    assert tt.due(store, THREAD_SESSION_ID, ep["id"]) is None, "no reply yet: nothing to name"
    _say(store, "hey jarvis", "Good evening, sir.")
    assert tt.due(store, THREAD_SESSION_ID, ep["id"]) is None, "a greeting alone: nothing to name yet"
    _say(store, "my gpu runs hot in elden ring", "Game mode would help.")
    upto = tt.due(store, THREAD_SESSION_ID, ep["id"])
    assert upto is not None

    namer = _Namer('"GPU heat in Elden Ring."')
    head = [{"role": "system", "content": "head"}, {"role": "user", "content": "my gpu runs hot in elden ring"}]
    assert await tt.name_topic(namer, "main", head, "Game mode would help.", [{"t": 1}], store,
                               THREAD_SESSION_ID, ep["id"], upto) == "GPU heat in Elden Ring"
    sent, kw = namer.calls[0]
    assert sent[:2] == head and sent[-1]["content"] == tt.TITLE_ASK, "the turn's own context, then the ask"
    assert kw["tools"] == [{"t": 1}] and kw["thinking"] is False, "same tools: the cached head still matches"
    assert tt.due(store, THREAD_SESSION_ID, ep["id"]) is None, "named: not due again yet"

    for i in range(tt.RETITLE_EVERY):
        _say(store, f"more {i}", "ok")
    assert tt.due(store, THREAD_SESSION_ID, ep["id"]) is not None, "renamed as the topic moves on"


def test_a_reply_that_is_not_a_name_is_dropped():
    from app.memory.thread_titles import clean_title
    assert clean_title("Title: Trip to Ooty.") == "Trip to Ooty"
    assert clean_title("Sure! This conversation was mostly about planning a two day trip to Ooty and hotels") is None
    assert clean_title("") is None


@pytest.mark.asyncio
async def test_a_closed_topic_keeps_its_record_title(store):
    from app.memory import thread_titles as tt
    eps = ThreadEpisodes(store)
    ep = eps.context(THREAD_SESSION_ID).episode
    _say(store, "hi", "hello")
    upto = tt.due(store, THREAD_SESSION_ID, ep["id"])
    eps.context(THREAD_SESSION_ID, new_topic=True)  # closes it before the name arrives
    store.update_episode(ep["id"], title="Greeting")
    assert await tt.name_topic(_Namer("Something else"), "main", [], "hello", None, store,
                               THREAD_SESSION_ID, ep["id"], upto) is None
    assert [e for e in store.list_episodes(THREAD_SESSION_ID) if e["id"] == ep["id"]][0]["title"] == "Greeting"


def test_device_2026_10_20_fixes():
    from app.agent.orchestrator import claims_unrun_forget, nudge_unrun_forget
    from app.memory.thread_titles import clean_title
    # "forget the previous topic" -> "Done -- the previous topic is forgotten." with no call
    assert claims_unrun_forget("Done -- the previous topic is forgotten. Fresh slate.", "forget the previous topic", [])
    assert not claims_unrun_forget("Done -- forgotten.", "forget the previous topic", [{"tool": "forget_conversation"}])
    assert not claims_unrun_forget("I haven't forgotten your trip.", "what about my trip?", [])
    assert "forget_conversation" in nudge_unrun_forget(["recall", "forget_conversation"])
    assert "forget_conversation" not in nudge_unrun_forget(["forget"])
    # a filler word in front of "new topic"
    assert split_new_topic("so lets start a new topic about unreal engine") == (True, "about unreal engine")
    assert split_new_topic("ok so, new topic") == (True, "")
    # a name that names the app, not the talk
    assert clean_title("Freeform conversation, no project") is None
    assert clean_title("Notes and tasks check") == "Notes and tasks check"
