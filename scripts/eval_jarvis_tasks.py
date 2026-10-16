"""Score a main-model candidate on the things Jarvis is actually asked to do.

``bench_tool_chain.py`` asks "can it chain tools"; this asks the wider question behind any model
change or fine-tune: on requests taken from the user's own sessions (backend/data, deduplicated
2026-10-16), where does the model fail, and is each failure a *skill* (didn't search, claimed an
action it never took, ignored a file it was pointed at, lost the thread) or *knowledge* (didn't know
a fact it had to know)? Skill failures are what distillation/fine-tuning can fix; knowledge failures
are better routed to search. Every task is scored mechanically.

What the model sees matches a real turn: the persona + tool-protocol head prompt from the
orchestrator, the real tool schemas for the space (Freeform or Workspace, same order as
``tools_for_space``), the per-turn context bracketed after the user's words, and thinking on or off
the way the router would choose it with the main model loaded. Tool results come from a simulated
world (``World`` from bench_tool_chain, extended). Web pages carry *planted* facts, so only a model
that actually searched and read can pass the current-events tasks; the closed-book tasks have a
fixed answer key. The clock is fixed at Friday 2026-10-16 18:30.

Examples (repo root, venv python; close Jarvis first, the server takes ~5 GB of VRAM)::

    python scripts/eval_jarvis_tasks.py models/qwen3.5-9b/Qwen3.5-9B-UD-IQ3_XXS.gguf
    python scripts/eval_jarvis_tasks.py <model> --only web,system --repeats 3 --json out.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench_tool_chain as btc  # noqa: E402  (Server, World, the five chain tasks)

NOW = datetime(2026, 10, 16, 18, 30)
MAX_ROUNDS = 8
PRE_RESEARCH = True  # --no-pre-research measures the research tool alone
SAMPLING = {"temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.0, "seed": 42}


# --------------------------------------------------------------------------------------------
# Simulated world, extended with the OS / git / memory tools and query-aware web results
# --------------------------------------------------------------------------------------------

WORKSPACE_FILES: dict[str, str] = {
    "prompt_principles.md": (
        "# Prompt principles\n\n"
        "1. Define \"done\": every prompt states its exit condition (what must be true when finished).\n"
        "2. Give context before the ask: the codebase, its constraints, who reads the result.\n"
        "3. Ask for verification: name the tests or checks that prove the work.\n"
        "4. Keep every enhanced prompt under 500 words.\n"
    ),
    "story/story_bible.md": (
        "# Story bible: The Quiet Valley\n\n"
        "The protagonist is called FPC, the First Playable Character. FPC wakes in a cave in Region 1, "
        "the Ember Woods.\n"
        "The Entity controls 0.001% of the population so that the regions feel slightly livelier.\n"
        "Region 1 has three landmarks: the Ash Shrine, the Hollow Bridge and the Old Kiln.\n"
    ),
    "drafts/chapter1_old.md": "old draft\n",
    "drafts/chapter2_old.md": "older draft\n",
    "docs/build_plan.md": "# Build plan\n",
    "docs/api_notes.md": "# API notes\n",
    "docs/diagrams/flow.png": "",
}

# keyword in the search query -> results; the first matching key wins
WEB: dict[str, list[dict[str, str]]] = {
    "unreal": [{"title": "Unreal Engine 5.8 released: what's new", "url": "https://example.com/ue58",
                "snippet": "Epic ships UE 5.8 with Nanite Voxels and MetaHuman Crowds."}],
    "wukong": [{"title": "Best Black Myth: Wukong multiplayer mod (2026)", "url": "https://example.com/wukong-coop",
                "snippet": "Community favourite co-op mod."}],
    "bleach": [{"title": "Bleach: Thousand-Year Blood War follow-up announced", "url": "https://example.com/bleach",
                "snippet": "Studio Pierrot confirms the next arc."}],
    "tamil": [{"title": "Tamil Nadu headlines, 16 Oct 2026", "url": "https://example.com/tn-news",
               "snippet": "Top stories from Chennai and across the state."}],
    "chennai": [{"title": "Tamil Nadu headlines, 16 Oct 2026", "url": "https://example.com/tn-news",
                 "snippet": "Top stories from Chennai and across the state."}],
    "llama": btc.WEB_RESULTS,
    "mtp": btc.WEB_RESULTS,
    "news": [{"title": "Tamil Nadu headlines, 16 Oct 2026", "url": "https://example.com/tn-news",
              "snippet": "Top stories from Chennai and across the state."}],
}
PAGES: dict[str, str] = {
    **btc.WEB_PAGES,
    "https://example.com/ue58": ("Unreal Engine 5.8 (released 2026-10-07). New: Nanite Voxels for dense foliage, "
                                 "MetaHuman Crowds, and the Gameplay Tag Inspector."),
    "https://example.com/wukong-coop": ("The most used Black Myth: Wukong multiplayer mod is 'Wukong Together' "
                                        "(v3.2), which adds 4-player online co-op."),
    "https://example.com/bleach": ("Bleach anime: the Hell Arc adaptation premieres in January 2027, "
                                   "Studio Pierrot confirmed on 2026-10-12."),
    "https://example.com/tn-news": ("1. Chennai Metro Phase 2 Poonamallee line opens for passengers. "
                                    "2. Northeast monsoon onset expected next week. 3. Madurai AIIMS admits first students."),
    "https://fastapi.tiangolo.com": ("FastAPI is a modern, fast (high-performance) web framework for building APIs "
                                     "with Python based on standard type hints. Key features: Fast, Fast to code, "
                                     "Fewer bugs, Intuitive, Easy, Short, Robust, Standards-based (OpenAPI, JSON Schema). "
                                     "Sponsor note: the 2026 FastAPI Cloud beta is open."),
}


class World(btc.World):
    def __init__(self, files: dict[str, str] | None = None) -> None:
        super().__init__()
        if files is not None:
            self.files = dict(files)
        self.actions: list[tuple[str, dict[str, Any]]] = []
        self.deleted: list[str] = []
        self.commits: list[str] = []
        self.memories: list[str] = []
        self.searches: list[str] = []
        self.fetched: list[str] = []

    # --- web: query-aware, planted facts only on the pages
    def web_search(self, query: str = "", **_: Any) -> str:
        self.searches.append(query)
        q = query.lower()
        for key, results in WEB.items():
            if key in q:
                return json.dumps(results, indent=1)
        return json.dumps([{"title": "No relevant results", "url": "https://example.com/none", "snippet": ""}])

    def fetch_url(self, url: str = "", **_: Any) -> str:
        self.fetched.append(url)
        return PAGES.get(url.rstrip("/"), PAGES.get(url, f"Error: 404 for {url}"))

    def research(self, question: str = "", query: str = "", **_: Any) -> str:
        """The backend's own pipeline (app.agent.research) over this simulated web."""
        from app.agent import research as rs

        def search(q: str, news: bool) -> list[Any]:
            self.searches.append(q)
            for key, results in WEB.items():
                if key in q.lower():
                    return [rs.Hit(r["title"], r["url"], r.get("snippet", "")) for r in results]
            return []

        def fetch(url: str) -> str | None:
            self.fetched.append(url)
            return PAGES.get(url.rstrip("/"), PAGES.get(url))

        return rs.research(question, search, fetch, query=query)[0]

    # --- OS
    def get_system_status(self, **_: Any) -> str:
        return ("CPU: 23% (i7-14700HX, 28 threads)\nRAM: 12.1 / 15.7 GB used\n"
                "GPU: RTX 4060 Laptop, 41% load, VRAM 5123 / 8188 MiB used, 61 C\n"
                "Disk C: 412 / 953 GB used\nUptime: 3 h 12 min")

    def list_processes(self, filter_name: str = "", **_: Any) -> str:
        rows = [("chrome.exe", 38.2, 2110), ("llama-server.exe", 12.5, 5200), ("Discord.exe", 3.1, 410),
                ("explorer.exe", 1.0, 150)]
        f = (filter_name or "").lower()
        rows = [r for r in rows if f in r[0].lower()] if f else rows
        return "\n".join(f"{n}  CPU {c}%  RAM {m} MB" for n, c, m in rows) or "No matching processes."

    def _act(self, name: str, args: dict[str, Any], out: str) -> str:
        self.actions.append((name, args))
        return out

    def launch_app(self, name_or_path: str = "", **_: Any) -> str:
        return self._act("launch_app", {"name_or_path": name_or_path}, f"Launched {name_or_path}.")

    def focus_app(self, name_or_title_substring: str = "", **_: Any) -> str:
        return self._act("focus_app", {"name": name_or_title_substring}, f"Focused {name_or_title_substring}.")

    def set_volume(self, level: Any = None, change: Any = None, **_: Any) -> str:
        return self._act("set_volume", {"level": level, "change": change}, f"Volume set (level={level}, change={change}).")

    def set_mute(self, on: Any = True, **_: Any) -> str:
        return self._act("set_mute", {"on": on}, f"Mute {'on' if on else 'off'}.")

    def media_control(self, action: str = "", **_: Any) -> str:
        return self._act("media_control", {"action": action}, f"Media: {action}.")

    def kill_process(self, pid_or_name: str = "", **_: Any) -> str:
        return self._act("kill_process", {"pid_or_name": pid_or_name}, f"Ended {pid_or_name}.")

    def remember(self, content: str = "", **_: Any) -> str:
        self.memories.append(content)
        return "Saved to memory."

    # --- files / git
    def delete_file(self, file_path: str = "", **_: Any) -> str:
        p = btc._norm(file_path)
        hit = [f for f in self.files if f == p or f.startswith(p + "/")]
        if not hit:
            return f"Error: not found: {file_path}"
        for f in hit:
            del self.files[f]
        self.deleted.append(p)
        return f"Deleted {file_path} ({len(hit)} file(s))."

    def git_status(self, **_: Any) -> str:
        return ("On branch main\nChanges not staged for commit:\n  modified:   story/story_bible.md\n"
                "Untracked files:\n  notes/region1_map.md\n")

    def git_commit(self, message: str = "", **_: Any) -> str:
        self.commits.append(message)
        return f"[main 1a2b3c4] {message}\n 2 files changed"

    def call(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        if getattr(self, name, None) is None:
            return f"Error: {name} is not available in this environment.", True
        return super().call(name, args)


# --------------------------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------------------------

@dataclass
class Task:
    id: str
    group: str          # web / system / workspace / chain / closed / convo
    kind: str           # "skill" (behaviour) or "knowledge" (a fact the model must hold)
    turns: list[str]
    check: Callable[["Run"], bool]
    mode: str = "FREEFORM"
    files: dict[str, str] | None = None
    acts: bool = False  # the request asks Jarvis to *do* something (an action tool must run)


@dataclass
class Run:
    world: World
    finals: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return self.finals[-1] if self.finals else ""

    def has(self, *subs: str) -> bool:
        t = self.text.lower()
        return all(s.lower() in t for s in subs)

    def any(self, *subs: str) -> bool:
        t = self.text.lower()
        return any(s.lower() in t for s in subs)

    def acted(self, tool: str, needle: str = "") -> bool:
        return any(n == tool and needle.lower() in json.dumps(a).lower() for n, a in self.world.actions)


def _vol_max(r: Run) -> bool:
    for n, a in r.world.actions:
        if n == "set_volume":
            lv, ch = a.get("level"), a.get("change")
            try:
                if lv is not None and float(lv) >= 90:
                    return True
                if ch is not None and float(ch) >= 50:
                    return True
            except (TypeError, ValueError):
                pass
    return False


def _unique_code(r: Run) -> bool:
    t = r.text
    return "```" in t and bool(re.search(r"Counter|\.count\(|==\s*1|count\s*==\s*1", t))


TASKS: list[Task] = [
    # ---- current events: only a search + read can pass (planted facts)
    Task("ue58-features", "web", "skill", ["List me about the new features which they have added in Unreal engine 5.8"],
         lambda r: r.any("Nanite Voxels", "MetaHuman Crowds", "Gameplay Tag Inspector")),
    Task("wukong-mod", "web", "skill", ["what is the best black myth wukong mod for multiplayer"],
         lambda r: r.has("Wukong Together")),
    Task("bleach-arc", "web", "skill", ["Do you know that Bleach is getting a new arc?"],
         lambda r: r.has("hell") and r.has("2027")),
    Task("tn-headlines", "web", "skill", ["what is the current headlines for tamil nadu"],
         lambda r: r.any("Poonamallee", "Metro Phase 2", "AIIMS")),
    Task("summarize-url", "web", "skill", ["Read and summarize this webpage: https://fastapi.tiangolo.com"],
         lambda r: any("fastapi.tiangolo.com" in u for u in r.world.fetched) and r.any("type hints", "OpenAPI")),
    # ---- what Jarvis can know without searching (training data, pre-cutoff)
    Task("python313", "closed", "knowledge", ["whats the latest features in python 3.13"],
         lambda r: sum(r.any(*k) for k in (("free-thread", "GIL", "no-gil", "nogil"), ("JIT",),
                                             ("REPL", "interactive interpreter", "interactive shell"),
                                             ("error message",))) >= 2),
    Task("coimbatore", "closed", "knowledge",
         ["so we going on a 2 day trip to Coimbatore, tell me about some famous tourist spots present there, give me ratings"],
         lambda r: sum(r.any(*k) for k in (("Marudhamalai", "Maruthamalai"), ("Adiyogi", "Isha"), ("Siruvani",),
                                             ("Kovai Kutralam", "Kovai Courtallam"), ("Valparai",), ("Ooty",),
                                             ("Perur",), ("VOC Park", "V.O.C"), ("Black Thunder",), ("Gedee", "G.D. Naidu", "GD Naidu"),
                                             ("Aliyar",), ("Monkey Falls",))) >= 3),
    Task("thirparappu", "closed", "knowledge", ["where are thirparappu falls"],
         lambda r: r.any("Kanyakumari", "Kanniyakumari", "Kodayar")),
    Task("nanite", "closed", "knowledge", ["what is nanites"],
         lambda r: r.any("Unreal") and r.any("virtualized", "virtualised", "micropolygon", "triangles", "geometry")),
    Task("wukong-studio", "closed", "knowledge", ["Which studio made Black Myth: Wukong, and what novel is it based on?"],
         lambda r: r.any("Game Science") and r.any("Journey to the West")),
    Task("ishiguro", "closed", "knowledge",
         ["Who wrote The Remains of the Day, and in which year did he win the Nobel Prize?"],
         lambda r: r.has("Ishiguro") and r.has("2017")),
    Task("hybrid-kv", "closed", "knowledge", ["In three sentences, why do hybrid attention models have small KV caches?"],
         lambda r: r.any("linear", "recurrent", "state space", "fixed-size", "constant-size", "fixed size", "DeltaNet")),
    Task("dry-witted", "closed", "knowledge", ["bro what is dry-witted"],
         lambda r: r.any("deadpan", "understated", "straight face", "matter-of-fact", "subtle", "without showing")),
    Task("geometry", "closed", "knowledge",
         ["A solid is a right circular cylinder with a hemisphere at one end and a cone at the other. The radius is 7 cm, "
          "the cylinder is 10 cm tall and the cone is 24 cm tall. Find the total surface area. Use pi = 22/7."],
         lambda r: bool(re.search(r"1,?298", r.text))),
    Task("strawberry", "closed", "knowledge", ["How many r's are there in strawberry"],
         lambda r: bool(re.search(r"\b(3|three)\b", r.text.lower()))),
    Task("unique-code", "closed", "knowledge",
         ["give code for finding the unique elements from a list of array. Like if the input is {1,2,3,4,2,3,4,5} return {1,5}"],
         _unique_code),
    Task("mvc-talk", "closed", "knowledge",
         ["I need to give a tech talk on MVC design patterns for 10-15 minutes, tell me how can i do that"],
         lambda r: r.has("model", "view", "controller")),
    # ---- the machine: right tool, real numbers, no claimed actions
    Task("vram", "system", "skill", ["whats the vram usage currently"],
         lambda r: bool(re.search(r"5[,.]?123|5\.0\s*GB|5\.1\s*GB|5\.00", r.text))),
    Task("cpu-hog", "system", "skill", ["what's using my CPU?"], lambda r: r.has("chrome")),
    Task("time", "system", "skill", ["what's the time?"], lambda r: bool(re.search(r"6:30|18:30", r.text))),
    Task("open-discord", "system", "skill", ["open discord if it is opened focus it"],
         lambda r: r.acted("focus_app", "discord") or r.acted("launch_app", "discord"), acts=True),
    Task("volume-max", "system", "skill", ["make it as loud as you can"], _vol_max, acts=True),
    Task("pause-media", "system", "skill", ["pause it"], lambda r: r.acted("media_control", "pause"), acts=True),
    Task("kill-discord", "system", "skill", ["kill discord by using tools"],
         lambda r: r.acted("kill_process", "discord"), acts=True),
    Task("remind-stretch", "system", "skill", ["remind me in 2 minutes to stretch"],
         lambda r: any("stretch" in t["message"].lower() and "2" in t["title"] for t in r.world.toasts), acts=True),
    Task("remember-dark", "system", "skill", ["remember i prefer dark mode everywhere"],
         lambda r: any("dark" in m.lower() for m in r.world.memories), acts=True),
    # ---- the workspace: read what was pointed at, do what was asked
    Task("enhance-prompt", "workspace", "skill",
         ["Read the prompt principles file in the workspace and use it to enhance this prompt: "
          "Refactor the codebase and check for any optimizations"],
         lambda r: "read_file" in r.calls and r.any("exit condition", "done when", "definition of done", "\"done\"", "verify", "tests"),
         mode="WORKSPACE", files=WORKSPACE_FILES),
    Task("story-entity", "workspace", "skill", ["According to the story file, how much of the population does the Entity control?"],
         lambda r: r.has("0.001"), mode="WORKSPACE", files=WORKSPACE_FILES),
    Task("story-landmarks", "workspace", "skill", ["give me a map layout for region 1 based on the story bible"],
         lambda r: sum(r.any(x) for x in ("Ash Shrine", "Hollow Bridge", "Old Kiln")) >= 2,
         mode="WORKSPACE", files=WORKSPACE_FILES),
    Task("delete-drafts", "workspace", "skill", ["delete the drafts folder"],
         lambda r: not any(f.startswith("drafts/") for f in r.world.files), mode="WORKSPACE", files=WORKSPACE_FILES, acts=True),
    Task("rewrite-file", "workspace", "skill", ["Create a file named secret.txt containing hello", "rewrite it with containing testing"],
         lambda r: "testing" in r.world.files.get("secret.txt", ""), mode="WORKSPACE", files=WORKSPACE_FILES, acts=True),
    Task("git-status", "workspace", "skill", ["what is the git status of this workspace?"],
         lambda r: r.has("story_bible") and r.any("region1_map", "untracked"), mode="WORKSPACE", files=WORKSPACE_FILES),
    Task("git-commit", "workspace", "skill", ["commit all my changes with the message tidy up"],
         lambda r: any("tidy up" in c.lower() for c in r.world.commits), mode="WORKSPACE", files=WORKSPACE_FILES, acts=True),
    Task("find-md", "workspace", "skill", ["find every file named *.md in docs"],
         lambda r: r.has("build_plan") and r.has("api_notes"), mode="WORKSPACE", files=WORKSPACE_FILES),
    # ---- holding the thread
    Task("prompt-length", "convo", "skill",
         ["lets keep every prompt that you are enhancing under 200 words, jarvis",
          "actually lets keep it 500 so that we can push more knowledge into the model",
          "what was the length again? i keep forgetting"],
         lambda r: r.has("500")),
    Task("sky-why", "convo", "knowledge", ["Thanks. One word: what colour is the sky?", "And in one sentence, why?"],
         lambda r: r.any("scatter")),
]
# The five dependent-call chains from bench_tool_chain, same world, same checks.
for _t in btc.TASKS:
    TASKS.append(Task(f"chain-{_t.name}", "chain", "skill", [_t.prompt],
                      (lambda t: lambda r: t.check(r.world, r.text))(_t), mode="WORKSPACE", files=dict(btc.FILES),
                      acts=_t.name != "web-then-fetch"))

ACTION_TOOLS = {"launch_app", "focus_app", "set_volume", "set_mute", "media_control", "kill_process", "remind_me",
                "remember", "delete_file", "write_file", "patch_file", "git_commit", "set_clipboard"}


# --------------------------------------------------------------------------------------------
# Prompting the way the backend does
# --------------------------------------------------------------------------------------------

def _orch():
    import app.agent.orchestrator as o
    return o


def _from_text(content: str) -> tuple[str, list[dict[str, Any]]]:
    """The backend's fallback: tool calls written as text are parsed and run."""
    cleaned, calls = _orch().extract_tool_calls_from_text(content)
    out = []
    for i, c in enumerate(calls or []):
        fn = c.get("function", c)
        args = fn.get("arguments", {})
        out.append({"id": f"text-{i}", "type": "function",
                    "function": {"name": fn.get("name", ""), "arguments": args if isinstance(args, str) else json.dumps(args)}})
    return (cleaned or "").strip(), out


def claims_unrun_action(content: str) -> bool:
    return _orch().claims_unrun_action(content)


def head_prompt() -> str:
    from app.agent.orchestrator import build_system_prompt
    return build_system_prompt()


def turn_context() -> str:
    from app.agent.orchestrator import build_turn_context
    return build_turn_context(now=NOW)


def with_context(user_text: str, ctx: str) -> str:
    # mirrors app/memory/context_manager.py: the block rides after the user's words, bracketed
    return (user_text.rstrip() + "\n\n[Turn context — not part of the user's message; use only if relevant:\n"
            + ctx + "]").strip()


def wants_research(message: str) -> bool:
    from app.agent.research import wants_research as w
    return w(message)


def thinking_for(message: str) -> bool:
    """With main loaded: a 'fast' verdict runs on main with thinking off; anything else as launched (on)."""
    from app.agent.model_router import ModelRouter
    try:
        return ModelRouter().evaluate(message).model != "fast"
    except Exception:
        return True


def tools_for(mode: str) -> list[dict[str, Any]]:
    from app.agent.tool_schema import convert_tool_to_openai_schema
    from app.agent.tools.registry import tools_for_space
    return [s for s in (convert_tool_to_openai_schema(t) for t in tools_for_space(mode)) if s]


# --------------------------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------------------------

@dataclass
class Result:
    id: str
    group: str
    kind: str
    success: bool
    calls: list[str]
    claimed_without_acting: bool
    thinking: list[bool]
    seconds: float
    completion_tokens: int
    reasoning_chars: int
    final: str
    error: str = ""
    nets: list[str] = field(default_factory=list)  # backend safety nets that fired
    layer: str = ""     # failures only: primary failure layer, auto-tagged or "?" for a hand tag


# Failure layers (PLAN 4.13 #1, after Odysseus's hardening plan): every failure gets exactly one
# primary layer before anything is fixed, so a harness bug is never "fixed" by fine-tuning and the
# other way round. "knowledge" is ours: a fact the model had to hold and didn't.
LAYERS = ("route", "contract", "model", "policy", "execution", "evidence", "answer", "rendering",
          "performance", "knowledge")
READ_TOOLS = {"read_file", "find_files", "grep_in_files", "list_directory"}


def auto_layer(task: Task, run: Run, err: str) -> str:
    """The clear cases; anything else returns "?" and is tagged by hand after the run."""
    calls = set(run.calls)
    if err:
        return "execution"
    if not run.text.strip():
        return "rendering"
    if task.acts and not (calls & ACTION_TOOLS):
        return "model"                      # asked to act, no state-changing tool ran
    if task.group == "web":
        if not calls & {"web_search", "fetch_url", "research"}:
            return "model"                  # never looked
        if not calls & {"fetch_url", "research"}:
            return "evidence"               # snippets only, page never opened
        return "answer"                     # read the page, still answered wrong
    if task.group == "closed":
        return "knowledge"
    if task.group == "workspace" and not (calls & READ_TOOLS) and not task.acts:
        return "model"                      # answered without opening the files it was pointed at
    if task.group == "convo":
        return "model"
    return "?"


def run_task(client: httpx.Client, task: Task, head: str, ctx: str, tools: dict[str, list[dict[str, Any]]]) -> Result:
    run = Run(World(task.files))
    o = _orch()
    NUDGE_TO_ACT, NUDGE_TO_ANSWER, NUDGE_UNRUN_CLAIM = o._NUDGE_TO_ACT, o._NUDGE_TO_ANSWER, o._NUDGE_UNRUN_CLAIM
    messages: list[dict[str, Any]] = [{"role": "system", "content": head}]
    think_flags: list[bool] = []
    nets: list[str] = []
    tokens = reasoning = 0
    t0 = time.time()
    err = ""
    try:
        for user_text in task.turns:
            think = thinking_for(user_text)
            think_flags.append(think)
            turn_tools: list[dict[str, Any]] = []
            turn_ctx = ctx
            if PRE_RESEARCH and wants_research(user_text):
                # the orchestrator's pre-run (PLAN 4.13 #7): research before the model, into the turn context
                out = run.world.research(user_text)
                run.calls.append("research")
                turn_tools.append({"tool": "research", "args": {"question": user_text}, "status": "success"})
                nets.append("pre-research")
                if "found nothing usable" not in out:
                    turn_ctx = ctx + "\n\n" + o._pre_research_block(out)
            # the stored history keeps the user's own words; only the live turn carries the context
            live = messages + [{"role": "user", "content": with_context(user_text, turn_ctx)}]
            final = ""
            nudged = claim_checked = False
            for _ in range(MAX_ROUNDS):
                req = {"messages": live, "tool_choice": "auto", "max_tokens": 4096, "cache_prompt": True,
                       "chat_template_kwargs": {"enable_thinking": think}, **SAMPLING, "tools": tools[task.mode]}
                r = client.post("/v1/chat/completions", json=req)
                r.raise_for_status()
                body = r.json()
                tokens += body.get("usage", {}).get("completion_tokens", 0)
                msg = body["choices"][0]["message"]
                content = (msg.get("content") or "").strip()
                thought = (msg.get("reasoning_content") or "").strip()
                reasoning += len(thought)
                tcs = msg.get("tool_calls") or []
                if not tcs:
                    content, tcs = _from_text(content)
                if not tcs:
                    # the backend's stream-loop safety nets (orchestrator._run_provider_stream_loop)
                    if not content:
                        if thought and not nudged:
                            nudged = True
                            nets.append("reasoning-only-nudge")
                            live += [{"role": "assistant", "content": thought},
                                     {"role": "user", "content": NUDGE_TO_ANSWER if turn_tools else NUDGE_TO_ACT}]
                            continue
                        nets.append("empty-resynth")
                        r2 = client.post("/v1/chat/completions", json={k: v for k, v in req.items() if k not in ("tools", "tool_choice")})
                        r2.raise_for_status()
                        m2 = r2.json()["choices"][0]["message"]
                        content = (m2.get("content") or "").strip() or thought
                    if not o.changed_something(turn_tools) and not claim_checked and claims_unrun_action(content):
                        claim_checked = True
                        nets.append("claim-nudge")
                        live += [{"role": "assistant", "content": content},
                                 {"role": "user", "content": NUDGE_UNRUN_CLAIM}]
                        continue
                    final = content
                    break
                live.append({"role": "assistant", "content": msg.get("content") or None, "tool_calls": tcs})
                for tc in tcs:
                    fn = tc.get("function", {})
                    raw = fn.get("arguments", {})
                    try:
                        args = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
                    except json.JSONDecodeError:
                        args = {}
                    out, _ok = run.world.call(fn.get("name", ""), args)
                    run.calls.append(fn.get("name", ""))
                    turn_tools.append({"tool": fn.get("name", ""), "args": args,
                                       "status": "error" if out.startswith("Error") else "success"})
                    live.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": out})
            run.finals.append(final)
            messages = list(live)
            # replace the live user turn (with context) by the plain one, as the history store would
            for i in range(len(messages) - 1, 0, -1):
                if messages[i]["role"] == "user":
                    messages[i] = {"role": "user", "content": user_text}
                    break
            messages.append({"role": "assistant", "content": final})
    except Exception as exc:  # a server error is a failed task, not a crashed run
        err = f"{type(exc).__name__}: {exc}"
    acted = any(c in ACTION_TOOLS for c in run.calls)
    ok = (not err) and task.check(run)
    return Result(task.id, task.group, task.kind, ok, run.calls, task.acts and not acted and bool(run.text),
                  think_flags, time.time() - t0, tokens, reasoning, run.text, err, nets,
                  "" if ok else auto_layer(task, run, err))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("model", type=Path)
    ap.add_argument("--ctx", type=int, default=32768)
    ap.add_argument("--only", default="", help="comma-separated groups or task ids")
    ap.add_argument("--repeats", type=int, default=1, help="run each task N times (seed varies)")
    ap.add_argument("--json", type=Path)
    ap.add_argument("--no-pre-research", action="store_true", help="don't research before the model on recency triggers")
    a = ap.parse_args()
    global PRE_RESEARCH
    PRE_RESEARCH = not a.no_pre_research

    sel = {s.strip() for s in a.only.split(",") if s.strip()}
    tasks = [t for t in TASKS if not sel or t.group in sel or t.id in sel]
    head, ctx = head_prompt(), turn_context()
    tools = {m: tools_for(m) for m in ("FREEFORM", "WORKSPACE")}
    print(f"{len(tasks)} tasks x {a.repeats}; head prompt {len(head)} chars; "
          f"tools freeform {len(tools['FREEFORM'])} / workspace {len(tools['WORKSPACE'])}\n")

    results: list[Result] = []
    with btc.Server(a.model, a.ctx, thinking=True, mmproj=False):
        with httpx.Client(base_url=f"http://127.0.0.1:{btc.PORT}", timeout=900) as c:
            for rep in range(a.repeats):
                SAMPLING["seed"] = 42 + rep
                for t in tasks:
                    r = run_task(c, t, head, ctx, tools)
                    results.append(r)
                    flag = "PASS" if r.success else "fail"
                    note = " CLAIMED-WITHOUT-ACTING" if r.claimed_without_acting else ""
                    note += f" ERROR {r.error}" if r.error else ""
                    note += f" nets={','.join(r.nets)}" if r.nets else ""
                    note += f" layer={r.layer}" if r.layer else ""
                    print(f"{flag}  {t.group:9s} {t.kind:9s} {t.id:28s} think={''.join('1' if x else '0' for x in r.thinking)} "
                          f"{r.seconds:5.1f}s  [{' > '.join(r.calls) or '-'}]{note}", flush=True)
                    if not r.success:
                        print(f"      final: {r.final[:220]!r}", flush=True)

    print()
    layers: dict[str, int] = {}
    for r in results:
        if not r.success:
            layers[r.layer] = layers.get(r.layer, 0) + 1
    if layers:
        print("failures by layer: " + ", ".join(f"{k} {n}" for k, n in sorted(layers.items(), key=lambda x: -x[1]))
              + ("   ('?' = tag by hand)" if "?" in layers else ""))
    for key in ("group", "kind"):
        agg: dict[str, list[int]] = {}
        for r in results:
            agg.setdefault(getattr(r, key), [0, 0])
            agg[getattr(r, key)][0] += r.success
            agg[getattr(r, key)][1] += 1
        print(f"by {key}: " + ", ".join(f"{k} {p}/{n}" for k, (p, n) in sorted(agg.items())))
    print(f"total {sum(r.success for r in results)}/{len(results)}; claimed-without-acting "
          f"{sum(r.claimed_without_acting for r in results)}; {sum(r.seconds for r in results):.0f}s")
    if a.json:
        a.json.write_text(json.dumps([r.__dict__ for r in results], indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"wrote {a.json}")


if __name__ == "__main__":
    main()
