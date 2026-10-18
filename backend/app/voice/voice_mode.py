"""
Voice mode (PLAN 4.9): Laya decides, code acts, MiniCPM talks.

While voice mode is on, the 9B is unloaded and the fast slot (MiniCPM5-2B) is served instead,
next to the Laya sidecar. Every addressed utterance goes through ``handle``:

- ``command_brain.evaluate`` asks Laya what the user wants (action + arguments, all selected,
  nothing generated) -- per part, when it is a compound ("open notepad and type buy milk");
- a confident plan runs here, through the same permission gate as a model's tool call, and
  comes back as a short line for Kokoro to say ("Opening Google Chrome.");
- a plan that needs a yes (typing into a terminal, closing a tab) asks out loud and waits for the
  next utterance to be yes or no;
- small talk goes to MiniCPM as a chat turn (route "chat"); questions, unsure plans and apps Laya
  couldn't place go to MiniCPM *with tools* (route "act") -- the client sends those as a normal
  chat turn carrying ``voice_route``;
- "write a reply saying..." (compose) is written by MiniCPM here and typed into the focused window.

Every plan is appended to ``backend/data/voice_plans.jsonl`` -- the user's real utterances are
the next fine-tuning and test set.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from app.config import settings
from app.voice import command_brain as brain

logger = logging.getLogger("jarvis.voice.mode")

PLAN_LOG = Path(__file__).resolve().parent.parent.parent / "data" / "voice_plans.jsonl"

_YES = re.compile(r"^\s*(?:yes|yeah|yep|yup|sure|ok(?:ay)?|go ahead|do it|confirm(?:ed)?|please do|go for it)\b", re.I)
_NO = re.compile(r"^\s*(?:no|nope|nah|don'?t|do not|cancel|stop|never ?mind|forget it)\b", re.I)

# Shortcuts the gate asks about (input_control.DESTRUCTIVE_KEYS by name) -> how the question
# starts; the window they're pinned to finishes it: "Close Visual Studio Code?"
_ASKED_SHORTCUTS = {"close_window": "Close", "close_tab": "Close the tab in", "cut": "Cut the selection in"}

_ASK_TIME = re.compile(r"^(?:what(?:'s| is)\s+the\s+time|what\s+time\s+is\s+it|(?:tell me\s+)?the\s+time)(?:\s+(?:now|right now|please))?\W*$", re.I)
_ASK_DATE = re.compile(r"^(?:what(?:'s| is)\s+(?:the\s+date|today'?s\s+date|the\s+day|today)|what\s+day\s+is\s+(?:it|today))(?:\s+(?:today|please))?\W*$", re.I)


def clock_answer(utterance: str, now: Optional[datetime] = None) -> str:
    """The time or date, said straight away, for the plain ways of asking; else ''."""
    u = (utterance or "").strip()
    now = now or datetime.now()
    if _ASK_TIME.match(u):
        return f"It's {now.strftime('%I:%M %p').lstrip('0')}."
    if _ASK_DATE.match(u):
        return f"It's {now.strftime('%A, %B')} {now.day}."
    return ""


# "What's my CPU usage?" on device (2026-10-04) got a per-process list; replayed, MiniCPM picked
# the right tool 18 of 24 times and otherwise offered to check, or made a number up. Plain usage
# questions are answered in code from a fresh sample, like the clock. "Which apps use the most
# CPU" is a different question (list_processes) and still goes to MiniCPM.
_ASKS_WHICH = re.compile(r"\b(?:which|what(?:'s| is)\s+using|apps?|programs?|process(?:es)?|eating|hogging)\b", re.I)
_ASKS_USAGE = re.compile(
    r"\b(?:usage|use|using|used|load|loaded|percent(?:age)?|busy|how much|how hot|temp(?:erature)?s?|status|at)\b", re.I)
_PART = {
    "cpu": re.compile(r"\b(?:cpu|processor)\b", re.I),
    "ram": re.compile(r"\b(?:ram|memory)\b", re.I),
    "gpu": re.compile(r"\b(?:gpu|vram|graphics(?:\s+card)?)\b", re.I),
    "temp": re.compile(r"\b(?:how hot|temp(?:erature)?s?)\b", re.I),
    "all": re.compile(r"\b(?:system|pc|computer|laptop|machine)\s+(?:status|usage|load|doing)\b|"
                      r"\bhow(?:'s| is)\s+(?:my|the)\s+(?:system|pc|computer|laptop|machine)\b", re.I),
}


_DEVICE = re.compile(r"\b(?:pc|computer|laptop|machine|system)\b", re.I)
_OURS = re.compile(r"\b(?:my|the|this|our)\s+(?:cpu|processor|ram|memory|gpu|vram|graphics|pc|computer|laptop|"
                   r"machine|system)|\bam\s+i\b", re.I)


def _gb(mb: float) -> str:
    return f"{mb / 1024:.1f}".rstrip("0").rstrip(".")


def _status_parts(utterance: str) -> list[str]:
    """Which readings a plain usage question about this machine asks for; [] for anything else."""
    u = (utterance or "").strip()
    if not u or _ASKS_WHICH.search(u):
        return []
    parts = [k for k, rx in _PART.items() if rx.search(u)]
    if parts == ["temp"] and not _DEVICE.search(u):
        return []  # "what's the temperature outside?"
    if not parts or ("all" not in parts and not (_ASKS_USAGE.search(u) and _OURS.search(u))):
        return []  # ...and "how much is a new CPU" is shopping, not this machine
    return parts


def asks_status(utterance: str) -> bool:
    """A plain usage question about this machine ("what's my GPU temperature / VRAM usage?")."""
    return bool(_status_parts(utterance))


def status_answer(utterance: str, snapshot: Callable[[], Any]) -> str:
    """A spoken hardware answer for the plain usage questions; '' for anything else. ``snapshot``
    returns an awareness ``SystemSnapshot`` (called only when the question matches)."""
    parts = _status_parts(utterance)
    if not parts:
        return ""
    s = snapshot()
    gpu_bits = []
    if s.gpu_available:
        gpu_bits.append(f"the GPU is at {s.gpu_util_percent:.0f}% with {_gb(s.vram_used_mb)} of "
                        f"{_gb(s.vram_total_mb)} gigabytes of VRAM in use")
        if s.gpu_temp_c is not None:
            gpu_bits.append(f"{s.gpu_temp_c:.0f} degrees")
    said = {
        "cpu": f"the CPU is at {s.cpu_percent:.0f}%",
        "ram": f"you're using {_gb(s.ram_used_mb)} of {_gb(s.ram_total_mb)} gigabytes of RAM",
        "gpu": ", ".join(gpu_bits) if gpu_bits else "I can't read the GPU right now",
        "temp": (f"the GPU is at {s.gpu_temp_c:.0f} degrees" if s.gpu_temp_c is not None
                 else "I can't read the GPU temperature right now"),
    }
    if "all" in parts:
        parts = ["cpu", "ram", "gpu"]
    if "temp" in parts and "gpu" in parts:
        parts.remove("temp")  # the GPU line already says how hot
    text = "; ".join(said[p] for p in parts)
    return text[0].upper() + text[1:] + "."


COMPOSE_SYSTEM = (
    "You write text the user will have typed into the window they are using. Reply with ONLY the "
    "text itself: no quotes, no preamble, no explanation. Keep it as short as the request allows."
)


@dataclass
class Step:
    """One executable piece of a plan: a registered tool function and its arguments."""
    tool: str
    args: dict[str, Any]
    say: str
    target: Any = None  # input_control.Window a needs-a-yes shortcut is pinned to


@dataclass
class VoiceOutcome:
    spoken: str = ""            # say this now (Kokoro); empty when the chat turn will speak
    route: Optional[str] = None  # "chat" | "act": the client sends ``query`` as a chat turn
    query: str = ""
    awaiting_confirmation: bool = False
    plans: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _Pending:
    steps: list[Step]
    utterance: str
    asked_at: float


class VoiceModeController:
    def __init__(self, predict: Optional[Callable[[dict, dict], dict]] = None,
                 apps: Optional[Callable[[], list[str]]] = None,
                 tools: Optional[dict[str, Callable[..., str]]] = None,
                 compose: Optional[Callable[[str], str]] = None,
                 snapshot: Optional[Callable[[], Any]] = None):
        self.enabled = False
        self._snapshot_fn = snapshot
        self.busy = ""             # "enabling" / "disabling" while a model swap runs
        self.error = ""
        self._predict = predict
        self._apps = apps
        self._tools = tools
        self._compose = compose
        self._pending: dict[str, _Pending] = {}
        self._lock = asyncio.Lock()
        self._switch: Optional[asyncio.Task] = None  # start()'s background switch, while it runs

    # ------------------------------------------------------------ wiring (lazy for tests)

    def _laya(self):
        from app.voice.laya_sidecar import get_laya_sidecar
        return get_laya_sidecar()

    def _snapshot(self):
        if self._snapshot_fn:
            return self._snapshot_fn()
        from app.main import awareness_monitor
        return awareness_monitor.collect_snapshot()  # fresh, not the 20 s poll

    def predict(self, state: dict, questions: dict) -> dict:
        return (self._predict or self._laya().predict)(state, questions)

    def apps(self) -> list[str]:
        if self._apps:
            return self._apps()
        from app.agent.tools.app_control import APP_ALIASES, URI_APPS, _start_menu_shortcuts
        return sorted(set(_start_menu_shortcuts()) | {k.title() for k in (*APP_ALIASES, *URI_APPS)})

    def tool(self, name: str) -> Callable[..., str]:
        if self._tools and name in self._tools:
            return self._tools[name]
        from app.agent.tools import input_control
        from app.agent.tools.registry import TOOL_FUNCTIONS
        return TOOL_FUNCTIONS.get(name) or getattr(input_control, name)

    def status(self) -> dict[str, Any]:
        try:
            from app.agent.runtime_process_manager import get_runtime_process_manager
            loaded = get_runtime_process_manager().current_model_kind
        except Exception:
            loaded = None
        laya = self._laya().status() if self._predict is None else {"running": True}
        return {"enabled": self.enabled, "busy": self.busy, "error": self.error,
                "loaded_model": loaded, "laya": laya}

    # ------------------------------------------------------------ on / off

    def start(self, enabled: bool) -> dict[str, Any]:
        """Begin turning voice mode on/off and return at once with ``busy`` set; the client polls
        ``status()``. A switch outlasts any sane HTTP timeout: 64 s on 2026-10-04, minutes when
        Whisper's GPU model has to download -- the app's 30 s request gave up and showed voice
        mode as off while the backend went on to turn it on."""
        if self.busy or (self.enabled == enabled and self._switch is None):
            return self.status()
        self.busy, self.error = ("enabling" if enabled else "disabling"), ""
        self._switch = asyncio.create_task(self._run_switch(enabled))
        return self.status()

    async def _run_switch(self, enabled: bool) -> None:
        try:
            await (self.enable() if enabled else self.disable())
        except Exception as e:  # the client only sees status(): say what broke there
            logger.exception("Voice mode %s failed", "on" if enabled else "off")
            self.error = f"Voice mode didn't switch {'on' if enabled else 'off'}: {e}"
        finally:
            self.busy, self._switch = "", None

    async def enable(self) -> dict[str, Any]:
        async with self._lock:
            if self.enabled:
                return self.status()
            self.busy, self.error = "enabling", ""
            try:
                from app.agent.runtime_process_manager import get_runtime_process_manager
                from app.governor.resource_governor import ActivityType
                from app.main import governor

                # Laya starts alongside the swap: its ~38 s cold start is Python + torch imports
                # on the CPU and it only takes VRAM at the end, long after the 9B is gone.
                # (In series, 2026-10-04: swap 18 s + Laya 38 s + Whisper 6 s = 64 s.)
                laya_task = asyncio.create_task(asyncio.to_thread(self._laya().ensure_running))
                try:
                    async with governor.activity(ActivityType.MODEL_LOADING, label="voice mode: fast"):
                        ok = await get_runtime_process_manager().switch_model("fast")
                except BaseException:
                    await asyncio.gather(laya_task, return_exceptions=True)
                    await asyncio.to_thread(self._laya().stop)  # voice mode stays off: no orphan
                    raise
                laya_ok = await laya_task
                if not ok:
                    self.error = "The fast model didn't load."
                if not laya_ok:
                    self.error = (self.error + " " if self.error else "") + \
                        "Laya didn't start; every command goes to MiniCPM."
                # Loaded last, into the room the 9B left; failing leaves the CPU model serving.
                from app.main import transcriber
                await asyncio.to_thread(transcriber.use_gpu, True)
                self.enabled = True
                logger.info("Voice mode on (fast model %s, Laya %s).", "up" if ok else "DOWN", "up" if laya_ok else "DOWN")
            finally:
                self.busy = ""
            return self.status()

    async def disable(self) -> dict[str, Any]:
        async with self._lock:
            if not self.enabled:
                return self.status()
            self.busy = "disabling"
            try:
                self.enabled = False
                self._pending.clear()
                await asyncio.to_thread(self._laya().stop)
                from app.agent.runtime_process_manager import get_runtime_process_manager
                from app.governor.resource_governor import ActivityType
                from app.main import governor, transcriber

                await asyncio.to_thread(transcriber.use_gpu, False)

                async with governor.activity(ActivityType.MODEL_LOADING, label="voice mode off: main"):
                    await get_runtime_process_manager().switch_model("main")
                logger.info("Voice mode off; main model back.")
            finally:
                self.busy = ""
            return self.status()

    # ------------------------------------------------------------ one utterance

    async def handle(self, utterance: str, session_id: str = "default") -> VoiceOutcome:
        utterance = brain.strip_wake_word(utterance)
        pending = self._pending.pop(session_id, None)
        if pending and time.time() - pending.asked_at < settings.confirmation_timeout_seconds:
            if _YES.match(utterance):
                spoken = await self._run_steps(pending.steps, session_id, pending.utterance, approved_first=True)
                return VoiceOutcome(spoken=spoken, awaiting_confirmation=session_id in self._pending)
            if _NO.match(utterance):
                return VoiceOutcome(spoken="Okay, I won't.")
            # anything else: drop the question and treat this as a new command

        clock = clock_answer(utterance)
        if clock:  # MiniCPM had the time in its context and still said it couldn't tell (2026-10-04)
            self._log(utterance, None, "clock")
            return VoiceOutcome(spoken=clock)
        try:
            status = await asyncio.to_thread(status_answer, utterance, self._snapshot)
        except Exception as e:  # no telemetry: MiniCPM with get_system_status takes it
            logger.warning("Voice status answer failed (%s)", e)
            status = ""
        if status:
            self._log(utterance, None, "status")
            return VoiceOutcome(spoken=status)

        try:
            plans = await asyncio.to_thread(self._evaluate_all, utterance)
        except Exception as e:  # Laya down or a bad answer: MiniCPM with tools takes it
            logger.warning("Voice brain failed (%s); handing to MiniCPM.", e)
            self._log(utterance, None, "laya_error", str(e))
            return VoiceOutcome(route="act", query=utterance)

        plan_dicts = [self._plan_dict(p) for p in plans]
        first = plans[0]
        if len(plans) == 1 and first.action == "none":
            self._log(utterance, first, "chat")
            return VoiceOutcome(route="chat", query=utterance, plans=plan_dicts)
        if len(plans) == 1 and first.action == "stop":
            self._log(utterance, first, "stop")
            asyncio.get_running_loop().create_task(self.disable())
            return VoiceOutcome(spoken="Voice mode off. Bringing the main model back.", plans=plan_dicts)

        steps: list[Step] = []
        for p in plans:
            low = p.confidence < settings.voice_mode_min_confidence
            if p.action in ("question", "none", "stop") or low:
                self._log(utterance, p, "act" + (" (low confidence)" if low else ""))
                return VoiceOutcome(route="act", query=utterance, plans=plan_dicts)
            if p.action == "compose":
                text = await asyncio.to_thread(self._write, p.utterance)
                if not text:
                    return VoiceOutcome(spoken="I couldn't write that just now.", plans=plan_dicts)
                steps.append(Step("type_text", {"text": text, "press_enter": bool(p.args.get("submit"))},
                                  "Written." if not p.args.get("submit") else "Written and sent."))
                continue
            step = self._step_for(p)
            if step is None:  # e.g. an app Laya couldn't place among the installed ones
                self._log(utterance, p, "act (no step)")
                return VoiceOutcome(route="act", query=utterance, plans=plan_dicts)
            steps.append(step)

        for p in plans:
            self._log(utterance, p, "run")
        spoken = await self._run_steps(steps, session_id, utterance)
        return VoiceOutcome(spoken=spoken, awaiting_confirmation=session_id in self._pending, plans=plan_dicts)

    def _evaluate_all(self, utterance: str) -> list[brain.Plan]:
        apps = self.apps()
        plan = brain.evaluate(utterance, self.predict, apps)
        if not plan.compound and not brain.looks_compound(utterance):
            return [plan]
        parts = brain.split_compound(utterance)
        if len(parts) < 2:
            return [plan]
        return [brain.evaluate(part, self.predict, apps) for part in parts]

    # ------------------------------------------------------------ plan -> steps

    def _step_for(self, p: brain.Plan) -> Optional[Step]:
        a = p.args
        if p.action == "open_app":
            if not a.get("app"):
                return None
            return Step("open_app", {"app": a["app"]}, f"Opening {a['app']}.")
        if p.action == "open_website":
            return Step("launch_app", {"name_or_path": a["url"]}, f"Opening {a['site']}.")
        if p.action == "web_search":
            url = brain.SEARCH_ENGINES[a["engine"]].format(q=urllib.parse.quote_plus(a["query"]))
            return Step("launch_app", {"name_or_path": url}, f"Searching {a['engine']} for {a['query']}.")
        if p.action == "type_text":
            return Step("type_text", {"text": a["text"], "press_enter": bool(a.get("submit"))}, "Typed.")
        if p.action == "shortcut":
            target = None
            if a["shortcut"] in _ASKED_SHORTCUTS:
                from app.agent.tools.input_control import target_window

                target = target_window()
            return Step("press_keys", {"keys": a["shortcut"]}, "", target)
        if p.action == "scroll":
            return Step("scroll", {"direction": a["direction"], "amount": a.get("amount") or "page"}, "")
        if p.action == "volume":
            op = a["op"]
            if op == "mute":
                return Step("set_mute", {"on": True}, "Muted.")
            if op == "unmute":
                return Step("set_mute", {"on": False}, "Sound's back on.")
            if op == "set_level":
                return Step("set_volume", {"level": a["level"]}, f"Volume {a['level']}.") if "level" in a else None
            if op == "max":
                return Step("set_volume", {"level": 100}, "Volume all the way up.")
            change = {"up": 10, "down": -10, "up_a_lot": 25, "down_a_lot": -25}[op]
            return Step("set_volume", {"change": change}, "")
        if p.action == "media":
            return Step("media_control", {"action": a["op"]}, "")
        if p.action == "open_folder":
            return Step("open_folder", {"folder": a["folder"]}, f"Opening {a['folder']}.")
        if p.action == "system":
            say = {"lock": "", "show_desktop": "", "screen_off": "", "screenshot": "Screenshot saved."}
            return Step("system_action", {"op": a["op"]}, say.get(a["op"], ""))
        if p.action == "reminder":
            when = (a.get("when") or "").strip()
            if not when or when.startswith("("):
                return None  # no time said: let MiniCPM ask
            when = re.sub(r"^for\s+", "in ", when, flags=re.I)
            text = a.get("text") or ""
            if not text or text.startswith("("):
                text = "Your timer is up"
            return Step("remind_me", {"text": text, "when": when}, f"Okay, {when}.")
        return None

    async def _run_steps(self, steps: list[Step], session_id: str, utterance: str, approved_first: bool = False) -> str:
        """Run steps in order through the permission gate. ``approved_first``: the user just said
        yes to steps[0], so it runs without asking again; later steps are still checked."""
        from app.agent import recent_objects
        from app.agent.permissions import evaluate_tool_permission

        if not approved_first:
            recent_objects.begin_turn(session_id)
        said: list[str] = []
        for i, step in enumerate(steps):
            gate_tool = "launch_app" if step.tool == "open_app" else step.tool
            gate_args = {"name_or_path": step.args["app"]} if step.tool == "open_app" else step.args
            if not (approved_first and i == 0) and gate_tool in ("launch_app", "type_text", "press_keys", "set_volume", "set_mute",
                             "media_control", "scroll", "remind_me"):
                decision = evaluate_tool_permission(gate_tool, gate_args, user_message=utterance)
                if not decision.allowed:
                    self._pending[session_id] = _Pending(steps[i:], utterance, time.time())
                    if step.tool == "press_keys" and step.args["keys"] in _ASKED_SHORTCUTS:
                        what = _ASKED_SHORTCUTS[step.args["keys"]]
                        where = step.target.label if step.target else "the window in front"
                        return " ".join(said + [f"{what} {where}?"])
                    reason = (decision.reason or "That needs your okay.").rstrip(".")
                    return " ".join(said + [f"{reason}. Should I go ahead?"])
            result = await asyncio.to_thread(self._execute, step)
            # The same record chat reads (PLAN 4.13 #4): a typed follow-up after a spoken
            # "pause it" can tell whether the pause really happened.
            recent_objects.record(session_id, {"tool": gate_tool, "args": gate_args, "result": result})
            if result.startswith("Error"):
                said.append("I couldn't do that: " + result.removeprefix("Error:").strip().split(". ")[0] + ".")
                break
            if step.say:
                said.append(step.say)
            elif step.target is not None:
                said.append(result)  # "Closed Visual Studio Code."
            if step.tool in ("open_app", "launch_app") and i + 1 < len(steps):
                # "open notepad and type hello": the next step's input must reach the new window,
                # not whatever had focus while it was starting.
                await asyncio.to_thread(self._wait_for_focus, step)
        return " ".join(said) or "Done."

    def _wait_for_focus(self, step: Step, timeout: float = 5.0) -> None:
        if self._tools:  # tests: no real windows
            return
        from app.agent.tools.input_control import foreground_window

        before = foreground_window()
        want = {w for w in re.findall(r"[a-z0-9]+", str(step.args.get("app") or "").lower()) if len(w) > 2}
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.2)
            now = foreground_window()
            text = f"{now[0]} {now[1]}".lower()
            if (want and any(w in text for w in want)) or (not want and now != before):
                time.sleep(0.4)  # the window is up; give its edit control a moment to take keys
                return
        logger.info("Voice mode: %s never took focus; continuing anyway.", step.args)

    def _execute(self, step: Step) -> str:
        if step.tool == "open_app":
            app = step.args["app"]
            focused = str(self.tool("focus_app")(app))
            if focused.startswith("Brought"):  # already open and now in front; else launch it
                return focused
            return self.tool("launch_app")(app)
        if step.target is not None:
            from app.agent.tools import input_control as ic

            if not ic.window_exists(step.target):
                return f"{step.target.label} is already closed."
            if step.args["keys"] == "close_window":
                return ic.close_window(step.target)
            if not ic.bring_to_front(step.target):
                return f"Error: couldn't bring {step.target.label} back to the front."
        return str(self.tool(step.tool)(**step.args))

    # ------------------------------------------------------------ compose

    def _write(self, instruction: str) -> str:
        if self._compose:
            return self._compose(instruction)
        try:
            r = httpx.post(
                f"http://127.0.0.1:{settings.llama_port}/v1/chat/completions",
                json={"messages": [{"role": "system", "content": COMPOSE_SYSTEM},
                                   {"role": "user", "content": instruction}],
                      "temperature": 0.6, "max_tokens": 400, "stream": False,
                      "chat_template_kwargs": {"enable_thinking": False}},
                timeout=60.0,
            )
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"] or ""
        except Exception as e:
            logger.warning("Compose failed: %s", e)
            return ""
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip().strip('"').strip()
        return text

    # ------------------------------------------------------------ log

    @staticmethod
    def _plan_dict(p: brain.Plan) -> dict[str, Any]:
        return {"utterance": p.utterance, "action": p.action, "args": p.args,
                "confidence": round(p.confidence, 3), "compound": p.compound, "ms": p.latency_ms}

    def _log(self, utterance: str, plan: Optional[brain.Plan], outcome: str, note: str = "") -> None:
        try:
            PLAN_LOG.parent.mkdir(parents=True, exist_ok=True)
            row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "utterance": utterance, "outcome": outcome}
            if plan is not None:
                row.update(self._plan_dict(plan))
                row["probs"] = (plan.answers.get("action") or {}).get("probabilities")
            if note:
                row["note"] = note
            with PLAN_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.debug("voice plan log failed: %s", e)


_controller: Optional[VoiceModeController] = None


def get_voice_mode() -> VoiceModeController:
    global _controller
    if _controller is None:
        _controller = VoiceModeController()
    return _controller
