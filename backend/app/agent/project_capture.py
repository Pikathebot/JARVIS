"""
The PROJECT.md safety net, the ``memory_capture`` pattern for ``project_state``. When a project
chat's message settles something ("we have decided...", "settled on", "the plan is") or orders the
work ("first X, next Y"), and the turn ended without a ``project_state`` call, one short side
request makes the model decide: ``project_state`` (one call per change) or ``nothing_to_record``,
with ``tool_choice=required``.

Why: on 2026-10-25 the user said "first we will look into orver's lore next we will look into the
battle system we have also decided that songs carry both the cutscene and the story". Ornith's
thinking planned "update the project state ... note the plan order", read the design document
first, and answered about the lore without ever coming back to it -- twice.

It sees the message and PROJECT.md as it is now, so it can tick an open Next item instead of
adding a twin; ``project_state`` refuses exact repeats itself.
"""
import asyncio
import logging
import re
from typing import Any, Optional

from app.agent.tools.project_state import project_state
from app.agent.tools.registry import execute_tool

logger = logging.getLogger("jarvis.agent.project_capture")

MAX_EDITS = 4

# Selects messages to ask about; the model still decides and words each line.
_PROJECT_CUE_RE = re.compile(
    r"\b(?:(?:we|i)(?:'ve| have| had)?(?: also| now| just)? (?:decided|agreed|settled|chose|picked)|"
    r"decided (?:to|that|on)|settled on|let'?s go with|we(?:'ll| will)? go with|the plan is|"
    r"first (?:we|i|let'?s)\b.{0,160}\b(?:then|next|after that|afterwards)|"
    r"(?:we|i)(?:'re| are| am|'m) (?:now )?(?:working on|moving on to|starting on)|"
    r"(?:we|i)(?:'ve| have)? (?:finished|completed|wrapped up)|(?:is|are) (?:done|finished) now)\b",
    re.IGNORECASE | re.DOTALL,
)

NOTHING_TO_RECORD = {
    "type": "function",
    "function": {
        "name": "nothing_to_record",
        "description": "The message changes nothing in the project's state.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

_SYSTEM = (
    "You keep this project's PROJECT.md current. Read the user's message below and record what it changes, "
    "one project_state call per change: add_decision for something settled (with the reason if given), "
    "add_next for each step of work they lined up, in their order, set_now for what they are working on now, "
    "tick when a listed Next step got done (quote it), add_done for finished work not in Next. Each text is "
    "one plain line that makes sense later. Skip what PROJECT.md already says. If the message changes "
    "nothing, call nothing_to_record."
)


def project_cue(user_message: str) -> bool:
    """True when the message reads like it settles or orders the project's work."""
    return bool(_PROJECT_CUE_RE.search(user_message or ""))


async def capture_project_state(
    provider: Any,
    model: str,
    user_message: str,
    tool_context: dict[str, Any],
    current_state: str = "",
) -> list[str]:
    """Ask the model to record or skip; returns the ``project_state`` results (empty when it
    skipped or anything failed -- a missed line is not worth an error)."""
    if not tool_context.get("project_id"):
        return []
    state = (current_state or "").strip() or "(empty)"
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": f"PROJECT.md now:\n{state}\n\nUser's message:\n{user_message}"},
    ]
    try:
        response = await provider.chat(
            messages=messages, model=model, tools=[project_state, NOTHING_TO_RECORD],
            temperature=0.2, thinking=False, tool_choice="required", timeout=60,
        )
    except Exception as e:
        logger.warning("PROJECT.md capture request failed: %s", e)
        return []
    calls = [c for c in (response.get("message") or {}).get("tool_calls") or []
             if (c.get("function") or {}).get("name") == "project_state"]
    if not calls:
        logger.info("PROJECT.md capture: nothing to record in %r", user_message[:80])
        return []
    results = []
    for call in calls[:MAX_EDITS]:
        args = (call.get("function") or {}).get("arguments") or {}
        if not isinstance(args, dict):
            continue
        result = await asyncio.to_thread(execute_tool, "project_state", args, tool_context)
        logger.info("PROJECT.md capture: %s (%s %r)", result, args.get("action"), args.get("text"))
        results.append(result)
    return results
