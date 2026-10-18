"""In-app compare: when a second take is made, the judge, the alternative and the votes (PLAN 4.13 #8)."""
import asyncio
import json
import random
import uuid

import pytest

from app.agent import compare


class JudgeThenWrite:
    """provider.chat: first the judge's word, then the alternative."""

    def __init__(self, judge="SHORTER", alternative="Game Science."):
        self.replies = [judge, alternative]
        self.calls = []

    async def chat(self, messages, model=None, thinking=None, **_):
        self.calls.append({"messages": messages, "thinking": thinking})
        return {"message": {"content": self.replies[len(self.calls) - 1]}}


MESSAGES = [{"role": "system", "content": "sys"}, {"role": "user", "content": "who made wukong?"}]


async def _finish():
    await asyncio.gather(*list(compare._tasks))


def test_eligibility():
    always = random.Random(0)
    assert compare.eligible("hi", [], False, False, False, 1.0, always)
    assert not compare.eligible("hi", [{"tool": "web_search"}], False, False, False, 1.0, always)  # a tool ran
    assert not compare.eligible("hi", [], True, False, False, 1.0, always)   # voice
    assert not compare.eligible("hi", [], False, True, False, 1.0, always)   # fast model
    assert not compare.eligible("hi", [], False, False, True, 1.0, always)   # off the record
    assert not compare.eligible("  ", [], False, False, False, 1.0, always)
    assert not compare.eligible("hi", [], False, False, False, 0.0, always)
    rng = random.Random(1)
    hits = sum(compare.eligible("hi", [], False, False, False, 1 / 15, rng) for _ in range(3000))
    assert 120 < hits < 280  # about 1 in 15


def test_parse_axis():
    assert compare.parse_axis("SHORTER") == "SHORTER"
    assert compare.parse_axis("I'd say: detailed.") == "DETAILED"
    assert compare.parse_axis("NONE") == "" and compare.parse_axis("") == "" and compare.parse_axis("maybe?") == ""


@pytest.mark.asyncio
async def test_a_named_axis_gives_a_steered_alternative_and_votes_are_saved(tmp_path):
    p = JudgeThenWrite()
    cid = compare.start(p, "main", "s1", "who made wukong?", MESSAGES,
                        "Black Myth: Wukong was developed by Game Science, based in Hangzhou...")
    assert compare.get(cid).public()["status"] == "judging"
    await _finish()
    pub = compare.get(cid).public()
    assert pub["status"] == "ready" and pub["axis"] == "shorter" and pub["alternative"] == "Game Science."
    judge, write = p.calls
    assert judge["thinking"] is False and judge["messages"][-1]["content"] == compare.JUDGE_PROMPT
    assert judge["messages"][:2] == MESSAGES  # same prefix: llama-server's cache covers it
    assert "much shorter" in write["messages"][-1]["content"] and write["messages"][0] == MESSAGES[0]

    votes = tmp_path / "votes.jsonl"
    assert compare.vote(cid, "alternative", path=votes) == {"saved": True}
    assert compare.vote(cid, "original", path=votes) is None  # once only
    rec = json.loads(votes.read_text(encoding="utf-8"))
    assert rec["source"] == "in-app" and rec["chosen"] == "Game Science." and rec["prompt"] == "who made wukong?"
    assert rec["rejected"].startswith("Black Myth") and rec["axis"] == "shorter" and not rec["tie"]


@pytest.mark.asyncio
async def test_none_means_no_card_and_dismiss_saves_nothing(tmp_path):
    cid = compare.start(JudgeThenWrite(judge="NONE"), "main", "s1", "q", MESSAGES, "a")
    await _finish()
    assert compare.get(cid).public() == {"id": cid, "status": "none"}
    assert compare.vote(cid, "original", path=tmp_path / "v.jsonl") is None

    cid = compare.start(JudgeThenWrite(), "main", "s1", "q", MESSAGES, "a long answer")
    await _finish()
    assert compare.vote(cid, "dismiss", path=tmp_path / "v.jsonl") == {"saved": False}
    assert not (tmp_path / "v.jsonl").exists()


@pytest.mark.asyncio
async def test_an_identical_alternative_or_an_error_shows_nothing():
    cid = compare.start(JudgeThenWrite(alternative="same  answer"), "main", "s", "q", MESSAGES, "same answer")
    await _finish()
    assert compare.get(cid).status == "none"

    class Broken:
        async def chat(self, **_):
            raise RuntimeError("server gone")
    cid = compare.start(Broken(), "main", "s", "q", MESSAGES, "a")
    await _finish()
    assert compare.get(cid).status == "failed"


@pytest.mark.asyncio
async def test_the_stream_offers_a_compare_only_on_a_text_reply(monkeypatch):
    from app.agent.orchestrator import AgentOrchestrator
    from app.config import settings
    from app.database import SessionLocal
    from app.main import run_db_migrations
    from app.memory.store import MemoryStore

    # The app's own migrations, not create_all: tables made without them left the shared test DB
    # unstamped, startup then skipped seeding, and later tests found no default project.
    run_db_migrations()
    monkeypatch.setattr(settings, "compare_rate", 1.0)
    started = []
    monkeypatch.setattr(compare, "start", lambda *a: started.append(a) or "cmp_x")
    orch = AgentOrchestrator(memory_store=MemoryStore(session_factory=SessionLocal))

    async def run(tools_used):
        async def fake_loop(*args, **kwargs):
            yield {"event": "done", "data": {"response": "Game Science.", "tools_used": tools_used}}
        orch._run_provider_stream_loop = fake_loop
        return [ev async for ev in orch.run_stream("who made wukong?", session_id=f"c-{uuid.uuid4().hex[:6]}",
                                                   chat_mode="FREEFORM", requested_model="main")][-1]

    assert (await run([]))["data"]["compare_id"] == "cmp_x"
    assert "compare_id" not in (await run([{"tool": "web_search"}]))["data"]
    assert len(started) == 1
