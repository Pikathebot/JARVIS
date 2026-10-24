"""Compaction the way Claude Code does it (PLAN 4.25)."""
import pytest

from app.agent.orchestrator import TurnMetrics
from app.memory import compaction
from app.memory.context_manager import ContextManager
from app.memory.thread import THREAD_SESSION_ID, ThreadEpisodes
from app.memory.token_counter import TokenCounter
from tests.test_thread import store  # noqa: F401  (fixture)

SUMMARY = "## What the user wants\n- Wuchang's system requirements.\n## Where it left off\n- Checking a 3050 laptop."


class FakeProvider:
    name = "llama_cpp"

    def __init__(self, reply=SUMMARY):
        self.reply, self.prompts = reply, []

    async def chat(self, messages, model=None, **kwargs):
        self.prompts.append(messages[-1]["content"])
        return {"message": {"role": "assistant", "content": self.reply}}


def _talk(store, session, n, size=40):
    for i in range(n):
        store.append_message(session, role="user", content=f"question {i} " * size)
        store.append_message(session, role="assistant", content=f"answer {i} " * size)


def test_command_and_trigger():
    assert compaction.parse_command("/compact") == ""
    assert compaction.parse_command("  /compact the calendar bits ") == "the calendar bits"
    assert compaction.parse_command("/compactor") is None and compaction.parse_command("compact it") is None
    assert compaction.should_compact(26300, 32768) and not compaction.should_compact(20000, 32768)
    assert not compaction.should_compact(None, 32768)


def test_peak_tokens_is_the_largest_request_of_the_turn():
    m = TurnMetrics()
    m.add_round({"timings": {"cache_n": 6000, "prompt_n": 500, "predicted_n": 80}})
    m.add_round({"timings": {"cache_n": 6500, "prompt_n": 9000, "predicted_n": 300}})
    assert m.payload([])["peak_tokens"] == 15800


def test_keep_point_keeps_the_last_exchange_at_least():
    rows = [{"id": i, "role": "user" if i % 2 == 0 else "assistant", "content": "x" * 400} for i in range(10)]
    assert compaction.keep_point(rows, 0, TokenCounter()) == 8
    assert compaction.keep_point(rows, 10_000, TokenCounter()) == 0  # everything fits: nothing to fold


@pytest.mark.asyncio
async def test_a_workspace_session_compacts_into_summary_plus_recent_rows(store):
    session = "ws-1"
    store.get_or_create_session(session, chat_mode="WORKSPACE")
    _talk(store, session, 30)
    provider = FakeProvider()
    notice = await compaction.compact(store, provider, session, focus="the laptop", manual=True)
    assert notice and notice["summary"] == SUMMARY and notice["manual"]
    assert "Claude" not in provider.prompts[0] and "## Where it left off" in provider.prompts[0]
    assert "focus on: the laptop" in provider.prompts[0]

    rows, pre = compaction.workspace_history(store, session)
    assert SUMMARY in pre
    talk = [r for r in rows if r["role"] != "system"]
    assert [r["content"].split()[1] for r in talk] == ["29", "29"]  # the last exchange only
    assert rows[-1]["name"] == "compaction" and rows[-1]["is_summary"]  # the divider, last

    pkg = ContextManager(memory_store=store).build_context(
        session_id=session, user_message="and on a 4060?", system_prompt="You are Jarvis.",
        max_context_tokens=32768, history_rows=rows, history_preamble=pre)
    roles = [m["role"] for m in pkg.messages]
    assert roles.count("system") == 1  # the marker is never replayed
    assert SUMMARY in pkg.messages[1]["content"]  # rides on the first kept user message
    assert "question 3 " not in str(pkg.messages)


@pytest.mark.asyncio
async def test_the_thread_compacts_into_its_episode_fold(store):
    episodes = ThreadEpisodes(store, TokenCounter())
    episodes.context(THREAD_SESSION_ID)
    _talk(store, THREAD_SESSION_ID, 30, size=300)
    notice = await compaction.compact(store, FakeProvider(), THREAD_SESSION_ID)
    assert notice and not notice["manual"]
    ctx = episodes.context(THREAD_SESSION_ID)
    assert SUMMARY in ctx.preamble
    assert 0 < len([r for r in ctx.rows if r["role"] == "user"]) < 30
    assert ctx.rows[-1]["name"] == "compaction"


@pytest.mark.asyncio
async def test_nothing_to_fold_and_an_empty_summary_change_nothing(store):
    session = "ws-2"
    store.get_or_create_session(session, chat_mode="WORKSPACE")
    _talk(store, session, 1)
    assert await compaction.compact(store, FakeProvider(), session, manual=True) is None
    _talk(store, session, 5)
    assert await compaction.compact(store, FakeProvider(reply=""), session, manual=True) is None
    assert compaction.workspace_history(store, session) is None


@pytest.mark.asyncio
async def test_a_background_compaction_is_announced_on_the_next_turn(store):
    session = "ws-3"
    store.get_or_create_session(session, chat_mode="WORKSPACE")
    _talk(store, session, 30, size=300)
    compaction.schedule(store, FakeProvider(), session, "main")
    import asyncio

    for _ in range(50):
        await asyncio.sleep(0.01)
        if session in compaction._notices:
            break
    notice = compaction.take_notice(session)
    assert notice and notice["summary"] == SUMMARY and compaction.take_notice(session) is None


@pytest.mark.asyncio
async def test_slash_compact_runs_now_and_says_so(store):
    from app.agent.orchestrator import AgentOrchestrator

    session = "ws-4"
    store.get_or_create_session(session, chat_mode="WORKSPACE")
    _talk(store, session, 12)
    # its own store: a default one creates the shared test database's tables before the
    # migrations seed it (test_database_schema then finds no default project)
    orch = AgentOrchestrator(provider=FakeProvider(), memory_store=store)
    events = [ev async for ev in orch.run_stream(user_message="/compact", session_id=session, chat_mode="WORKSPACE")]
    kinds = [e["event"] for e in events]
    assert kinds == ["tool_start", "tool_end", "compacted", "token", "done"]
    assert events[2]["data"]["manual"] is True
    assert "Compacted" in events[-1]["data"]["response"]
    # the command itself is not stored as a message; the divider is
    assert [r["name"] for r in store.get_message_rows(session) if r["role"] == "system"] == ["compaction"]
    assert not any(r["content"] == "/compact" for r in store.get_message_rows(session))
