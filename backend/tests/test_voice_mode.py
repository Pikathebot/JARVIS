"""Voice mode's controller: plan -> permission gate -> tool, spoken yes/no, routes to MiniCPM.

Laya, the tools and the writer are faked; the permission gate is the real one.
"""
import pytest

from app.agent import permissions
from app.agent.permissions import RiskTier, evaluate_tool_permission
from app.agent.tools import input_control
from app.voice import voice_mode
from app.voice.voice_mode import VoiceModeController

APPS = ["Google Chrome", "Discord", "Notepad", "Steam"]


class FakeLaya:
    """Stage 1 answers from ``script[utterance]``; stage-2 picks by question id."""

    def __init__(self, script: dict, confidence: float = 0.95):
        self.script, self.confidence, self.calls = script, confidence, 0

    def __call__(self, state, questions):
        self.calls += 1
        u = state["utterance"]
        action, compound, picks = self.script[u]
        out = {}
        for qid, q in questions.items():
            crit = q["criteria"]
            if qid == "action":
                pick = action
            elif qid == "compound":
                pick = "A" if compound else "B"
            else:
                pick = picks.get(qid, next(iter(crit)))
                if callable(pick):
                    pick = pick(crit)
            out[qid] = {"type": "choice", "choice": pick, "answer_confidence": self.confidence,
                        "probabilities": {k: float(k == pick) for k in crit}}
        return out


def span(text):
    return lambda crit: next(k for k, v in crit.items() if v == text)


class Tools:
    def __init__(self):
        self.calls = []

    def __getitem__(self, name):
        def fn(*args, **kwargs):
            self.calls.append((name, kwargs or args))
            if name == "focus_app":
                return "Error: No visible window matching it."
            return "ok"
        return fn

    def __contains__(self, name):
        return True


@pytest.fixture(autouse=True)
def _quiet(monkeypatch, tmp_path):
    monkeypatch.setattr(voice_mode, "PLAN_LOG", tmp_path / "voice_plans.jsonl")
    monkeypatch.setattr(input_control, "foreground_is_risky", lambda: None)
    monkeypatch.setattr(input_control, "target_window", lambda: None)


def controller(script, confidence=0.95, compose=None):
    tools = Tools()
    c = VoiceModeController(predict=FakeLaya(script, confidence), apps=lambda: APPS, tools=tools,
                            compose=compose or (lambda instruction: "Running late, sorry!"))
    c.enabled = True
    return c, tools


@pytest.mark.asyncio
async def test_open_app_runs_and_speaks():
    c, tools = controller({"open chrome": ("open_app", False, {"app": "Google Chrome"})})
    out = await c.handle("Jarvis, open chrome")
    assert out.route is None and out.spoken == "Opening Google Chrome."
    assert ("launch_app", ("Google Chrome",)) in tools.calls
    assert voice_mode.PLAN_LOG.read_text().count("\n") == 1


@pytest.mark.asyncio
async def test_small_talk_goes_to_chat_and_questions_to_act():
    c, _ = controller({"thanks jarvis": ("none", False, {}), "whats the weather": ("question", False, {})})
    assert (await c.handle("thanks jarvis")).route == "chat"
    out = await c.handle("whats the weather")
    assert out.route == "act" and out.query == "whats the weather"


@pytest.mark.asyncio
async def test_low_confidence_hands_the_utterance_to_minicpm():
    c, tools = controller({"bring up steam": ("media", False, {"media_op": "play"})}, confidence=0.3)
    out = await c.handle("bring up steam")
    assert out.route == "act" and not tools.calls


@pytest.mark.asyncio
async def test_compound_runs_each_part_in_order():
    c, tools = controller({
        "open notepad and type buy milk": ("open_app", True, {}),
        "open notepad": ("open_app", False, {"app": "Notepad"}),
        "type buy milk": ("type_text", False, {"text": span("buy milk"), "submit": "B"}),
    })
    out = await c.handle("open notepad and type buy milk")
    assert [name for name, _ in tools.calls] == ["focus_app", "launch_app", "type_text"]
    assert tools.calls[-1][1] == {"text": "buy milk", "press_enter": False}
    assert out.spoken == "Opening Notepad. Typed."


@pytest.mark.asyncio
async def test_typing_into_a_terminal_asks_then_runs_on_yes(monkeypatch):
    monkeypatch.setattr(input_control, "foreground_is_risky",
                        lambda: "The focused window is a terminal (pwsh.exe): typed text and Enter run as a command.")
    c, tools = controller({"type git status and press enter":
                           ("type_text", False, {"text": span("git status"), "submit": "A"})})
    out = await c.handle("type git status and press enter", "s1")
    assert out.awaiting_confirmation and "Should I go ahead?" in out.spoken and not tools.calls
    out = await c.handle("yes please", "s1")
    assert tools.calls == [("type_text", {"text": "git status", "press_enter": True})]
    assert out.spoken == "Typed."


@pytest.mark.asyncio
async def test_no_cancels_the_pending_action(monkeypatch):
    c, tools = controller({"close this tab": ("shortcut", False, {"shortcut": "close_tab"})})
    out = await c.handle("close this tab", "s1")
    assert out.awaiting_confirmation
    out = await c.handle("no", "s1")
    assert out.spoken == "Okay, I won't." and not tools.calls


@pytest.mark.asyncio
async def test_close_it_is_pinned_to_the_window_it_asked_about(monkeypatch):
    # On device the yes came 16 s after the question; focus had moved, Alt+F4 went elsewhere.
    code = input_control.Window(101, "Code.exe", "main.py - JARVIS - Visual Studio Code")
    monkeypatch.setattr(input_control, "target_window", lambda: code)
    monkeypatch.setattr(input_control, "window_exists", lambda w: True)
    closed = []
    monkeypatch.setattr(input_control, "close_window", lambda w: closed.append(w) or f"Closed {w.label}.")
    c, tools = controller({"close it": ("shortcut", False, {"shortcut": "close_window"})})
    out = await c.handle("close it", "s1")
    assert out.awaiting_confirmation and out.spoken == "Close Visual Studio Code?"
    monkeypatch.setattr(input_control, "target_window", lambda: None)  # focus moved meanwhile
    out = await c.handle("yes, go ahead", "s1")
    assert closed == [code] and not tools.calls  # by handle, not a keypress in whatever is in front
    assert out.spoken == "Closed Visual Studio Code."


@pytest.mark.asyncio
async def test_close_it_when_the_window_is_already_gone(monkeypatch):
    gone = input_control.Window(102, "notepad.exe", "Untitled - Notepad")
    monkeypatch.setattr(input_control, "target_window", lambda: gone)
    monkeypatch.setattr(input_control, "window_exists", lambda w: False)
    c, tools = controller({"close it": ("shortcut", False, {"shortcut": "close_window"})})
    await c.handle("close it", "s1")
    out = await c.handle("yes", "s1")
    assert out.spoken == "Notepad is already closed." and not tools.calls


def test_clock_answers_the_plain_ways_of_asking():
    from datetime import datetime

    now = datetime(2026, 10, 13, 9, 5)
    for said in ["What's the time?", "what time is it", "What is the time now?", "the time please"]:
        assert voice_mode.clock_answer(said, now) == "It's 9:05 AM.", said
    for said in ["What's the date?", "what day is it today", "What's today's date?"]:
        assert voice_mode.clock_answer(said, now) == "It's Tuesday, October 13.", said
    for said in ["what time does the store close", "set a timer for the time it takes", "what's the time in Tokyo"]:
        assert voice_mode.clock_answer(said, now) == "", said


@pytest.mark.asyncio
async def test_time_is_answered_without_laya_or_minicpm():
    c, tools = controller({})  # an empty script: any Laya call would raise
    out = await c.handle("Jarvis, what's the time?")
    assert out.route is None and out.spoken.startswith("It's ") and not tools.calls


@pytest.mark.asyncio
async def test_compose_writes_with_minicpm_and_types_it():
    c, tools = controller({"reply saying i'll be late": ("compose", False, {"submit": "B"})})
    out = await c.handle("reply saying i'll be late")
    assert tools.calls == [("type_text", {"text": "Running late, sorry!", "press_enter": False})]
    assert out.spoken == "Written."


@pytest.mark.asyncio
async def test_unplaced_app_and_laya_failure_go_to_minicpm():
    c, _ = controller({"open photoshop": ("open_app", False, {})})  # shortlist is empty -> app none
    assert (await c.handle("open photoshop")).route == "act"

    def broken(state, questions):
        raise ConnectionError("laya down")

    c2 = VoiceModeController(predict=broken, apps=lambda: APPS, tools=Tools())
    out = await c2.handle("open chrome")
    assert out.route == "act" and out.query == "open chrome"


@pytest.mark.asyncio
async def test_timer_becomes_a_reminder_in_n_minutes():
    c, tools = controller({"set a timer for 10 minutes":
                           ("reminder", False, {"remind_when": span("for 10 minutes")})})
    await c.handle("set a timer for 10 minutes")
    assert tools.calls == [("remind_me", {"text": "Your timer is up", "when": "in 10 minutes"})]


def test_gate_asks_for_destructive_keys_and_risky_windows(monkeypatch):
    assert evaluate_tool_permission("press_keys", {"keys": "copy"}).allowed
    assert not evaluate_tool_permission("press_keys", {"keys": "close_tab"}).allowed
    assert not evaluate_tool_permission("press_keys", {"keys": "alt+f4"}).allowed
    assert evaluate_tool_permission("type_text", {"text": "hi"}).allowed
    monkeypatch.setattr(input_control, "foreground_is_risky", lambda: "terminal")
    d = evaluate_tool_permission("type_text", {"text": "rm -rf"})
    assert not d.allowed and d.risk_tier == RiskTier.CONFIRMATION_REQUIRED and d.reason == "terminal"
    assert not evaluate_tool_permission("press_keys", {"keys": "enter"}).allowed
    assert evaluate_tool_permission("press_keys", {"keys": "copy"}).allowed


@pytest.mark.asyncio
@pytest.mark.parametrize("route, note_start", [("chat", "Voice mode: this reply is spoken aloud. Answer"),
                                               ("act", "Voice mode: this reply is spoken aloud. Use a tool")])
async def test_voice_turn_runs_on_fast_with_tools_and_its_note(monkeypatch, route, note_start):
    """A voice turn stays on MiniCPM (the 9B is unloaded) and keeps the same tool list either way,
    so its prompt prefix is stable; what the turn is for rides on the user turn."""
    from unittest.mock import AsyncMock, MagicMock

    from app.agent import orchestrator as orch_mod
    from tests.fake_providers import ClientBackedProvider

    sent = []
    client = MagicMock()
    monkeypatch.setattr(orch_mod.AgentOrchestrator, "_slot_has_vision", lambda self, slot, provider: False)

    async def reply(model=None, messages=None, tools=None):
        sent.append({"model": model, "messages": messages, "tools": tools})
        yield {"type": "token", "delta": "Sure."}
        yield {"type": "done", "content": "Sure.", "tool_calls": []}

    client.chat_stream = reply
    store = MagicMock()
    store.get_or_create_session.return_value = {"chat_mode": "FREEFORM"}
    store.get_messages.return_value = []
    skills = MagicMock(match_skills=MagicMock(return_value=[]), build_skill_prompt_injection=MagicMock(return_value=""))
    orch = orch_mod.AgentOrchestrator(provider=ClientBackedProvider(client), openrouter_client=MagicMock(),
                                      memory_store=store, compactor=MagicMock(compact=AsyncMock(return_value=([], None))),
                                      skills_loader=skills, mcp_manager=MagicMock(get_tool_definitions=MagicMock(return_value=[])))
    events = [ev async for ev in orch.run_stream(user_message="delete probe.txt", session_id="v1",
                                                  chat_mode="FREEFORM", voice_route=route)]
    assert "done" in [e["event"] for e in events]
    assert sent and "fast" in str(sent[0]["model"]).lower()
    assert sent[0]["tools"], "voice turns keep the space's tools"
    assert "look_at_screen" not in str(sent[0]["tools"]), "a blind MiniCPM isn't offered the screen"
    assert "launch_app" in str(sent[0]["tools"])
    user_turn = [m for m in sent[0]["messages"] if m["role"] == "user"][-1]["content"]
    assert note_start in user_turn
    assert orch_mod._FAST_NO_TOOLS_NOTE not in user_turn


def test_parse_combo():
    assert input_control.parse_combo("ctrl+shift+t") == [0x11, 0x10, ord("T")]
    with pytest.raises(ValueError):
        input_control.parse_combo("ctrl+banana")


class _SlowStarts:
    """Laya and the model swap, faked: the swap only finishes if Laya started meanwhile."""

    def __init__(self, swap_ok=True, swap_raises=False):
        import threading

        self.laya_started, self.stopped = threading.Event(), False
        self.swap_ok, self.swap_raises = swap_ok, swap_raises

    # Laya sidecar
    def ensure_running(self):
        self.laya_started.set()
        return True

    def stop(self):
        self.stopped = True

    def status(self):
        return {"running": self.laya_started.is_set() and not self.stopped}

    # runtime process manager
    current_model_kind = "fast"

    async def switch_model(self, kind):
        import asyncio

        seen = await asyncio.to_thread(self.laya_started.wait, 2.0)
        if self.swap_raises:
            raise RuntimeError("llama-server died")
        return self.swap_ok and seen


def _wire_enable(monkeypatch, fake):
    import app.agent.runtime_process_manager as rpm
    import app.main as main_mod

    monkeypatch.setattr(VoiceModeController, "_laya", lambda self: fake)
    monkeypatch.setattr(rpm, "get_runtime_process_manager", lambda: fake)
    monkeypatch.setattr(main_mod.transcriber, "use_gpu", lambda on: True)


@pytest.mark.asyncio
async def test_enable_starts_laya_while_the_model_swaps(monkeypatch):
    # In series on device (2026-10-04) it was swap 18 s + Laya 38 s + Whisper 6 s.
    fake = _SlowStarts()
    _wire_enable(monkeypatch, fake)
    c = VoiceModeController()
    status = await c.enable()
    assert c.enabled and status["error"] == "", status


@pytest.mark.asyncio
async def test_enable_stops_laya_when_the_swap_fails(monkeypatch):
    fake = _SlowStarts(swap_raises=True)
    _wire_enable(monkeypatch, fake)
    c = VoiceModeController()
    with pytest.raises(RuntimeError):
        await c.enable()
    assert not c.enabled and fake.stopped and c.busy == ""


@pytest.mark.asyncio
async def test_start_answers_at_once_and_the_switch_finishes_behind_it(monkeypatch):
    # The app's requests time out at 30 s; a switch took 64 s (2026-10-04), minutes with a download.
    import asyncio

    fake = _SlowStarts()
    _wire_enable(monkeypatch, fake)
    c = VoiceModeController()
    status = c.start(True)
    assert status["busy"] == "enabling" and not status["enabled"]
    assert c.start(True)["busy"] == "enabling"  # a second click mid-switch starts nothing new
    while c.busy:
        await asyncio.sleep(0.01)
    assert c.enabled and c.status()["error"] == ""
    assert c.start(True)["busy"] == ""  # already on: nothing to do


@pytest.mark.asyncio
async def test_a_failed_switch_reports_through_status(monkeypatch):
    import asyncio

    _wire_enable(monkeypatch, _SlowStarts(swap_raises=True))
    c = VoiceModeController()
    c.start(True)
    while c.busy:
        await asyncio.sleep(0.01)
    assert not c.enabled and "llama-server died" in c.status()["error"]


def test_laya_vram_is_its_own_count_not_the_cards_change():
    from app.voice import laya_sidecar

    class Health:
        def __init__(self, body):
            self.body = body

        def get(self, url, timeout=None):
            return type("R", (), {"json": lambda _self: self.body})()

    s = laya_sidecar.LayaSidecar.__new__(laya_sidecar.LayaSidecar)
    s.base_url = "http://127.0.0.1:8004"
    s._client = Health({"ok": True, "vram_mib": 916})
    assert s._reported_vram_mb() == 916 + laya_sidecar._CUDA_CONTEXT_MB
    s._client = Health({"ok": True})  # an older server: fall back to the card-wide reading
    assert s._reported_vram_mb() is None


def _snap(**kw):
    from app.awareness.observations import SystemSnapshot

    base = dict(cpu_percent=12.4, ram_used_mb=9830.0, ram_total_mb=16384.0, gpu_available=True,
                gpu_util_percent=3.0, vram_used_mb=5222.0, vram_total_mb=8188.0, gpu_temp_c=52.0)
    base.update(kw)
    return SystemSnapshot(**base)


def test_status_answers_the_plain_usage_questions():
    s = lambda: _snap()  # noqa: E731
    assert voice_mode.status_answer("What's the CPU percentage?", s) == "The CPU is at 12%."
    assert voice_mode.status_answer("what's my cpu usage", s) == "The CPU is at 12%."
    assert voice_mode.status_answer("How busy is the CPU right now?", s) == "The CPU is at 12%."
    assert voice_mode.status_answer("How much RAM am I using?", s) == "You're using 9.6 of 16 gigabytes of RAM."
    assert voice_mode.status_answer("how hot is the gpu", s) == \
        "The GPU is at 3% with 5.1 of 8 gigabytes of VRAM in use, 52 degrees."
    assert voice_mode.status_answer("How's my laptop doing?", s).startswith("The CPU is at 12%; you're using")
    # Bare readings count too (device test 2026-10-18: "GPU temperature?" got none)
    assert voice_mode.status_answer("GPU temperature?", s).endswith("52 degrees.")
    assert voice_mode.status_answer("cpu usage", s) == "The CPU is at 12%."
    for said in ["temperature?", "new CPU", "gpu prices", "gpu driver update"]:
        assert voice_mode.status_answer(said, s) == "", said
    # Not a usage question, or a "which app" one (list_processes, MiniCPM), or not about this machine.
    for said in ["Which apps are using the most CPU?", "what's eating my RAM", "open the gpu settings",
                 "what's the temperature outside?", "play some music", "how much is a new CPU"]:
        assert voice_mode.status_answer(said, s) == "", said


@pytest.mark.asyncio
async def test_status_is_answered_without_laya_or_minicpm():
    c = VoiceModeController(predict=FakeLaya({}), apps=lambda: APPS, tools=Tools(), snapshot=lambda: _snap())
    c.enabled = True
    out = await c.handle("Jarvis, what's my CPU usage?")
    assert out.route is None and out.spoken == "The CPU is at 12%."
