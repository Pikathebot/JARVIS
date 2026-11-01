"""
Unanswered confirmations: they lapse, Jarvis says so, and a late approval is refused.
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

from app.agent.confirmations import (
    CONFIRMATION_TIMEOUT_KIND,
    ConfirmationRegistry,
    ConfirmationWatcher,
    build_timeout_observation,
)
from app.awareness.monitor import AwarenessMonitor
from app.awareness.observations import Severity
from app.persona.profiles import JARVIS


def pending(action_id="act_1", tool="run_command", command="git push origin main"):
    return {"action_id": action_id, "tool": tool, "args": {"command": command},
            "risk_tier": "CONFIRMATION_REQUIRED", "reason": "test"}


@pytest.fixture
def registry():
    return ConfirmationRegistry(timeout_seconds=60.0)


# ------------------------------------------------------------------ registry

def test_ask_records_a_deadline_and_a_speakable_description(registry):
    entries = registry.ask("s1", [pending()], now=1000.0)

    assert len(entries) == 1
    assert entries[0].expires_at == 1060.0
    assert entries[0].description == "run command: git push origin main"
    assert [p.action_id for p in registry.pending("s1")] == ["act_1"]
    assert registry.pending("other") == []


def test_zero_timeout_means_asks_never_lapse():
    registry = ConfirmationRegistry(timeout_seconds=0)
    registry.ask("s1", [pending()], now=1000.0)

    assert registry.expire(now=10**9) == []
    assert registry.filter_approvals(["act_1"]) == (["act_1"], [])


def test_approval_inside_the_window_is_allowed_and_consumes_the_ask(registry):
    registry.ask("s1", [pending()], now=1000.0)

    assert registry.filter_approvals(["act_1"], now=1030.0) == (["act_1"], [])
    assert registry.pending() == []


def test_approval_after_the_window_is_refused(registry):
    registry.ask("s1", [pending()], now=0.0)  # deadline long past

    allowed, stale = registry.filter_approvals(["act_1"])

    assert allowed == [] and stale == ["act_1"]
    # ...and still refused once the watcher has swept it.
    assert registry.filter_approvals(["act_1"]) == ([], ["act_1"])


def test_a_token_this_registry_never_issued_approves_nothing(registry):
    # act_<hash> is a deterministic hash of the call, so anyone who can reach the API could compute
    # one for an action Jarvis never asked about (PLAN 4.29 A2). A restart's forgotten asks too.
    assert registry.filter_approvals(["act_unknown"], "s1") == ([], ["act_unknown"])


def test_a_token_is_good_only_in_the_session_that_was_asked_and_only_once(registry):
    registry.ask("s1", [pending()], now=1000.0)
    assert registry.filter_approvals(["act_1"], "other", now=1001.0) == ([], ["act_1"])
    assert registry.get("act_1") is not None  # still pending where it was asked
    assert registry.filter_approvals(["act_1"], "s1", now=1001.0) == (["act_1"], [])
    assert registry.filter_approvals(["act_1"], "s1", now=1002.0) == ([], ["act_1"])


@pytest.mark.asyncio
async def test_a_precomputed_token_for_an_unasked_action_is_asked_about_instead(monkeypatch):
    from app.agent.permissions import generate_action_id
    from app.agent.tools import registry as tools
    from test_orchestrator_tool_turns import ScriptedProvider, _orchestrator
    from test_taint import _call
    from unittest.mock import MagicMock

    ran = []
    monkeypatch.setitem(tools.TOOL_FUNCTIONS, "execute_command", lambda command, **_: ran.append(command) or "ok")
    args = {"command": "git push origin main"}
    forged = generate_action_id("execute_command", args)
    orch = _orchestrator()
    orch.memory_store = MagicMock()
    orch.provider = ScriptedProvider([_call("c1", "execute_command", args)])
    events = [ev async for ev in orch.run_stream("push it", session_id="forged-1", approved_action_ids=[forged])]
    assert ran == []
    assert events[-1]["event"] == "confirmation_required"
    assert [p["action_id"] for p in events[-1]["data"]["pending_confirmations"]] == [forged]


def test_re_asking_a_lapsed_action_is_flagged_so_the_prompt_can_say_why(registry):
    registry.ask("s1", [pending()], now=0.0)
    registry.expire(now=100.0)

    registry.ask("s1", [pending()], now=200.0)
    assert registry.last_ask_was_repeat is True

    registry.ask("s1", [pending("act_2")], now=200.0)
    assert registry.last_ask_was_repeat is False


def test_deny_drops_the_ask_quietly(registry):
    registry.ask("s1", [pending()], now=1000.0)

    assert registry.deny("act_1") is not None
    assert registry.deny("act_1") is None
    assert registry.expire(now=10**9) == []


# ------------------------------------------------------------------- spoken

def test_timeout_observation_names_the_action_in_the_persona_voice(registry):
    lapsed = registry.ask("s1", [pending()], now=0.0)

    observation = build_timeout_observation(lapsed, JARVIS)

    assert observation.kind == CONFIRMATION_TIMEOUT_KIND
    assert observation.severity == Severity.WARNING
    assert "run command: git push origin main" in observation.spoken
    assert "have not done it, sir" in observation.spoken
    assert observation.data["action_ids"] == ["act_1"]


def test_several_lapsed_actions_become_one_sentence(registry):
    lapsed = registry.ask("s1", [pending("a"), pending("b", "delete_file", "x")], now=0.0)

    observation = build_timeout_observation(lapsed, JARVIS)

    assert "2 actions" in observation.spoken
    assert observation.data["action_ids"] == ["a", "b"]


# ------------------------------------------------------------------ watcher

def test_watcher_announces_lapsed_asks_through_the_monitor_once(registry):
    monitor = AwarenessMonitor(governor=None)
    queue = monitor.subscribe()
    watcher = ConfirmationWatcher(registry, monitor, persona_provider=lambda: JARVIS)
    registry.ask("s1", [pending()], now=0.0)
    registry.ask("s2", [pending("act_9", "delete_file", "y")], now=0.0)

    emitted = watcher.sweep_once(now=100.0)

    assert len(emitted) == 2  # one per session, not one per action
    payload = queue.get_nowait()
    assert payload["kind"] == CONFIRMATION_TIMEOUT_KIND
    assert payload["speak"] is True
    assert watcher.sweep_once(now=200.0) == []


def test_watcher_stays_quiet_for_asks_still_inside_the_window(registry):
    monitor = AwarenessMonitor(governor=None)
    watcher = ConfirmationWatcher(registry, monitor, persona_provider=lambda: JARVIS)
    registry.ask("s1", [pending()], now=1000.0)

    assert watcher.sweep_once(now=1030.0) == []
    assert registry.pending("s1")


@pytest.mark.asyncio
async def test_watcher_loop_starts_and_stops_cleanly(registry):
    watcher = ConfirmationWatcher(
        registry, AwarenessMonitor(governor=None), persona_provider=lambda: JARVIS,
        check_seconds=0.01,
    )
    await watcher.start()
    await asyncio.sleep(0.03)
    await watcher.stop()
    assert watcher._task is None


# ------------------------------------------------------------------- prompt

def test_prompt_says_it_is_a_re_ask_after_a_timeout():
    from app.agent.permissions import build_confirmation_prompt

    prompt = build_confirmation_prompt([pending()], JARVIS, re_asked=True)

    assert prompt["spoken"].startswith("That approval had timed out, so I am asking again.")
    assert "Say yes to proceed" in prompt["spoken"]
    assert prompt["text"].startswith("_The earlier approval timed out")


# -------------------------------------------------------------------- router

def test_deny_endpoint_drops_a_pending_ask(monkeypatch):
    import app.agent.confirmations as mod
    from app.main import app

    fresh = ConfirmationRegistry(timeout_seconds=60.0)
    monkeypatch.setattr(mod, "_registry", fresh)
    fresh.ask("s1", [pending()])

    with TestClient(app) as client:
        listed = client.get("/api/confirmations", params={"session_id": "s1"}).json()
        assert [p["action_id"] for p in listed["pending"]] == ["act_1"]
        assert listed["pending"][0]["seconds_remaining"] > 0

        assert client.post("/api/confirmations/act_1/deny").status_code == 200
        assert client.post("/api/confirmations/act_1/deny").status_code == 404
        assert client.get("/api/confirmations").json()["pending"] == []
