"""
In-app compare: now and then, a second take on a reply for the user to vote on (PLAN 4.13 #8).

The offline review page compares two models on eval prompts; this collects the same
``{prompt, chosen, rejected}`` votes from real use, with one model and no model swap:

1. A reply finishes as usual. If no tool ran this turn (nothing would happen twice), it isn't
   voice, off the record or the fast model, and a ~1-in-15 draw passes, the ``done`` event carries
   a ``compare_id``.
2. In the background a short thinking-off *judge* call asks the model whether a meaningfully
   different reply would serve better, and along which axis (shorter / more casual / more detailed /
   restructured), or NONE -- most replies have nothing worth comparing.
3. If it names an axis, the alternative is generated steered along it. The client polls
   ``GET /api/compare/{id}`` and shows a compare card only once one is ready.
4. The vote (``POST /api/compare/{id}/vote``) is appended to ``data/preference_votes.jsonl``,
   the file the review page writes and #10 trains from.

Both requests reuse the turn's messages, so llama-server's prompt cache covers the shared prefix.
llama-server runs one slot: a message sent while an alternative is being written waits for it (a
few seconds, and it is rare).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("jarvis.agent.compare")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
VOTES_PATH = Path(os.environ.get("JARVIS_PREFERENCE_VOTES_PATH") or REPO_ROOT / "data" / "preference_votes.jsonl")
DEFAULT_RATE = 1 / 15
KEEP_SECONDS = 15 * 60  # an unanswered compare is forgotten after this

AXES: dict[str, str] = {
    "SHORTER": "shorter",
    "CASUAL": "more casual",
    "DETAILED": "more detailed",
    "RESTRUCTURE": "structured differently",
}
_STEER: dict[str, str] = {
    "SHORTER": "Answer again, much shorter: the essentials only.",
    "CASUAL": "Answer again in a more casual, conversational tone.",
    "DETAILED": "Answer again in more depth: the detail and examples a curious user would want.",
    "RESTRUCTURE": "Answer again with a clearly different structure (e.g. a list instead of prose, or the other way round).",
}
JUDGE_PROMPT = (
    "(Note from Jarvis's harness, not the user.) Look at your last reply. Would a noticeably different "
    "version serve the user better or at least as well? Reply with exactly one word: SHORTER, CASUAL, "
    "DETAILED, RESTRUCTURE, or NONE if your reply is already the right shape."
)


@dataclass
class Compare:
    id: str
    session_id: str
    prompt: str
    original: str
    model: str
    status: str = "judging"   # judging -> writing -> ready | none | failed; voted at the end
    axis: str = ""
    alternative: str = ""
    created: float = field(default_factory=time.monotonic)

    def public(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "status": self.status}
        if self.status in ("ready", "voted"):
            out.update(axis=AXES.get(self.axis, self.axis.lower()), alternative=self.alternative)
        return out


_lock = threading.Lock()
_compares: dict[str, Compare] = {}
_tasks: set[asyncio.Task] = set()


def _sweep() -> None:
    now = time.monotonic()
    for key in [k for k, c in _compares.items() if now - c.created > KEEP_SECONDS]:
        _compares.pop(key, None)


def eligible(answer: str, tools_used: list[Any], voice: bool, fast: bool, ephemeral: bool,
             rate: float, rng: Optional[random.Random] = None) -> bool:
    """A text-only reply on main, on the record, and the draw passes."""
    if voice or fast or ephemeral or tools_used or not (answer or "").strip() or rate <= 0:
        return False
    return (rng or random).random() < rate


def parse_axis(text: str) -> str:
    """The judge's word, or "" for NONE / anything unreadable."""
    for word in re.findall(r"[A-Z]+", (text or "").upper()):
        if word in AXES:
            return word
        if word == "NONE":
            return ""
    return ""


def steered(messages: list[dict[str, Any]], axis: str) -> Optional[list[dict[str, Any]]]:
    """The turn's messages with the steering note appended to the last user turn (Qwen3.5's template
    takes no second system message); None when that turn isn't plain text."""
    if not messages or messages[-1].get("role") != "user" or not isinstance(messages[-1].get("content"), str):
        return None
    last = dict(messages[-1])
    last["content"] = last["content"] + f"\n\n[Harness note, not the user: {_STEER[axis]}]"
    return messages[:-1] + [last]


def start(provider: Any, model: str, session_id: str, user_message: str, messages: list[dict[str, Any]],
          answer: str) -> str:
    """Register a compare and start the judge + alternative in the background; returns its id."""
    c = Compare(id=f"cmp_{uuid.uuid4().hex[:12]}", session_id=session_id, prompt=user_message,
                original=answer, model=model)
    with _lock:
        _sweep()
        _compares[c.id] = c
    task = asyncio.create_task(_run(provider, c, list(messages)))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return c.id


async def _run(provider: Any, c: Compare, messages: list[dict[str, Any]]) -> None:
    try:
        judged = await provider.chat(model=c.model, thinking=False, messages=messages + [
            {"role": "assistant", "content": c.original}, {"role": "user", "content": JUDGE_PROMPT}])
        axis = parse_axis((judged.get("message", {}) or {}).get("content", ""))
        if not axis:
            c.status = "none"
            return
        alt_messages = steered(messages, axis)
        if alt_messages is None:
            c.status = "none"
            return
        c.axis, c.status = axis, "writing"
        alt = await provider.chat(model=c.model, thinking=False, messages=alt_messages)
        text = ((alt.get("message", {}) or {}).get("content", "") or "").strip()
        if not text or " ".join(text.split()) == " ".join(c.original.split()):
            c.status = "none"
            return
        c.alternative, c.status = text, "ready"
        logger.info("Compare %s ready (%s, %d chars).", c.id, axis, len(text))
    except Exception as exc:  # a background extra: never surfaces as an error
        logger.warning("Compare %s failed: %s", c.id, exc)
        c.status = "failed"


def get(compare_id: str) -> Optional[Compare]:
    with _lock:
        return _compares.get(compare_id)


def vote(compare_id: str, choice: str, path: Path = VOTES_PATH) -> Optional[dict[str, Any]]:
    """original / alternative / tie save a vote; dismiss saves nothing. None when there's nothing
    to vote on (unknown, not ready, already voted)."""
    with _lock:
        c = _compares.get(compare_id)
        if c is None or c.status != "ready" or choice not in ("original", "alternative", "tie", "dismiss"):
            return None
        c.status = "voted"
    if choice == "dismiss":
        return {"saved": False}
    alt_label = f"{c.model}:{c.axis.lower()}"
    winner_alt = choice == "alternative"
    record = {
        "pair": c.id,
        "time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": "in-app",
        "session_id": c.session_id,
        "prompt": c.prompt,
        "earlier_turns": [],
        "chosen": c.alternative if winner_alt else c.original,
        "rejected": c.original if winner_alt else c.alternative,
        "chosen_model": alt_label if winner_alt else c.model,
        "rejected_model": c.model if winner_alt else alt_label,
        "axis": c.axis.lower(),
        "tie": choice == "tie",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return {"saved": True}
