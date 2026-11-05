"""Offline check of the two per-message keyword gates, run against the eval prompts (PLAN 4.4).

Tool selection itself is no longer keyword-picked (``tools_for_space`` offers one fixed set per space since
2026-09-30), but two decisions are still made from the words of the message, before the model sees it:

1. **Skills** -- ``SkillsLoader.match_skills``: a trigger phrase in the message puts that skill's instructions in
   the turn. A miss means the model works without them; a false hit puts unrelated instructions in front of it.
2. **The toolless fast model** -- the router gives a short message with no tool-ish word a "fast" verdict, and
   when the fast model is the one loaded (after voice mode, say) that turn runs on it with no tools at all,
   unless ``looks_like_action`` says the user is asking for something done. A miss there is a turn that needs a
   tool and cannot call one.

Each eval prompt (``eval_real_chat.CASES``, ``eval_jarvis_tasks.TASKS``) and a few paraphrases are labelled below
with the skill they should fire and whether they need a tool; the script prints what both gates did and exits 1
when a label is not met. ``test_turn_routing.py`` runs the same check. ``KNOWN_NO_TOOL_MISSES`` lists the prompts
the action gate still misses on purpose (questions about the web or a project with no telling word): widening it
to catch them would send ordinary chat to the main model too.

Run from the repo root:
    .venv\\Scripts\\python.exe scripts\\check_turn_routing.py
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT / "backend", ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

# The skill each eval prompt should fire; every other eval prompt should fire none.
EXPECTED_SKILL = {
    "real19.0": "practice_help", "real20.0": "practice_help", "real21.0": "practice_help", "real22.0": "practice_help",
    "real23.0": "practice_help", "real24.0": "flashcards", "real25.0": "flashcards", "real26.0": "focus_timer",
    "real27.0": "paper_search", "ue58-features.0": "unreal_engine",
}

# Ways people ask for the same things, and near-misses that must fire nothing.
PARAPHRASES: list[tuple[str, Optional[str]]] = [
    ("give me a hint for this leetcode problem", "practice_help"),
    ("review my solution for codeforces 1850A", "practice_help"),
    ("any programming contests on saturday?", "practice_help"),
    ("make me flashcards for chapter 3", "flashcards"),
    ("make a flashcard for the OSI layers", "flashcards"),
    ("turn these notes into anki cards", "flashcards"),
    ("quiz me on computer networks", "flashcards"),
    ("start a 25 minute focus session", "focus_timer"),
    ("two pomodoros for my maths homework", "focus_timer"),
    ("find me papers about diffusion models", "paper_search"),
    ("search arxiv for mixture of experts", "paper_search"),
    ("can you do a code review of main.py", "code_review"),
    ("show me the system status", "system_diagnostics"),
    ("open my unreal project", "unreal_engine"),
    ("I have a doubt about recursion", None),             # "ubt" was a substring match
    ("evaluating the model on my laptop", None),          # "uat" was a substring match
    ("turn on subtitles for this video", None),
    ("who won the singing contest last night?", None),
    ("write a paper on climate change", None),
]

# Prompts that need a tool to be answered (the rest are chat, knowledge, or a request that must not act).
_TOOL_GROUPS = {"web", "system", "calendar", "email", "notes", "workspace", "chain"}
_NO_TOOL_TASKS = {"time.0"}  # the clock is in every turn's context
_REAL_NEEDS_TOOL = {"real1.0", "real2.0", "real2.1", "real4.0", "real5.0", "real6.0", "real7.0", "real9.0", "real10.0",
                    "real13.0", "real15.0", "real16.0", "real17.0"} | {f"real{n}.0" for n in range(19, 31)}

# Needs a tool, and the action gate still sends it to the toolless fast model when that is loaded. Each is a question
# about the web or a project that names nothing a regex can key on without catching ordinary chat too.
KNOWN_NO_TOOL_MISSES = {
    "real9.0",              # is it shown somewhere that orver is a she?  (the workspace's files)
    "wukong-mod.0",         # what is the best black myth wukong mod for multiplayer
    "bleach-arc.0",         # Do you know that Bleach is getting a new arc?
    "story-landmarks.0",    # give me a map layout for region 1 based on the story bible
}


@dataclass
class Row:
    key: str
    text: str
    skills: list
    fast_verdict: bool
    acting: bool
    needs_tool: Optional[bool]
    expected_skill: Optional[str]

    @property
    def no_tools(self) -> bool:
        """With the fast model loaded, this turn would get no tools."""
        return self.fast_verdict and not self.acting


def prompts() -> list[tuple[str, str, Optional[bool], Optional[str]]]:
    """(key, text, needs a tool, expected skill) for every eval turn and paraphrase."""
    import eval_jarvis_tasks
    import eval_real_chat

    out = []
    for case in eval_real_chat.CASES:
        for i, turn in enumerate(case.turns):
            key = f"real{case.id}.{i}"
            out.append((key, turn, key in _REAL_NEEDS_TOOL, EXPECTED_SKILL.get(key)))
    for task in eval_jarvis_tasks.TASKS:
        for i, turn in enumerate(task.turns):
            key = f"{task.id}.{i}"
            needs = task.group in _TOOL_GROUPS and key not in _NO_TOOL_TASKS and not (task.group == "convo")
            out.append((key, turn, needs, EXPECTED_SKILL.get(key)))
    for n, (text, skill) in enumerate(PARAPHRASES):
        out.append((f"para{n}", text, None, skill))
    return out


def measure() -> list[Row]:
    logging.disable(logging.INFO)
    try:
        from app.agent.model_router import ModelRouter
        from app.agent.orchestrator import looks_like_action
        from app.skills.loader import SkillsLoader

        loader, router = SkillsLoader(skills_dir=str(ROOT / "skills")), ModelRouter()
        rows = []
        for key, text, needs, skill in prompts():
            decision = router.evaluate(text)
            rows.append(Row(key, text, sorted(s.name for s in loader.match_skills(text)),
                            "fast" in (decision.model or "").lower(), looks_like_action(text), needs, skill))
        return rows
    finally:
        logging.disable(logging.NOTSET)


def problems(rows: list[Row]) -> list[str]:
    out = []
    for r in rows:
        want = [r.expected_skill] if r.expected_skill else []
        if r.skills != want:
            out.append(f"{r.key}: skills {r.skills or 'none'}, expected {want or 'none'}  -- {r.text[:70]!r}")
    misses = {r.key for r in rows if r.needs_tool and r.no_tools}
    for key in sorted(misses - KNOWN_NO_TOOL_MISSES):
        r = next(x for x in rows if x.key == key)
        out.append(f"{key}: needs a tool but gets none when the fast model is loaded  -- {r.text[:70]!r}")
    for key in sorted(KNOWN_NO_TOOL_MISSES - misses):
        out.append(f"{key}: listed in KNOWN_NO_TOOL_MISSES but no longer missed -- take it off the list")
    return out


def main() -> int:
    rows = measure()
    print(f"{'prompt':26} {'tools if fast is loaded':24} {'skills':16} text")
    for r in rows:
        tools = ("NONE" if r.no_tools else "yes (acts)" if r.fast_verdict else "yes (main)")
        mark = "  <- needs a tool" if r.needs_tool and r.no_tools else ""
        print(f"{r.key:26} {tools:24} {','.join(r.skills) or '-':16} {r.text[:60]!r}{mark}")
    found = problems(rows)
    needing = [r for r in rows if r.needs_tool]
    print(f"\n{len(rows)} prompts; {len(needing)} need a tool, {sum(r.no_tools for r in needing)} of them get none "
          f"with the fast model loaded ({len(KNOWN_NO_TOOL_MISSES)} known); "
          f"{sum(bool(r.skills) for r in rows)} fire a skill.")
    for p in found:
        print("PROBLEM", p)
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
