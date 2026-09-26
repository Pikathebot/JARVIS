"""
The memory safety net. When a message reads like something lasting (``memory_cue``) and the
turn ended without the model calling ``remember``, one short side request makes it decide:
two tools, ``remember`` or ``nothing_to_remember``, and ``tool_choice=required``. The model
still judges and words the memory; it just can't drift into "Understood, sir." and save nothing.

Why: rule 20 in the prefix plus a per-turn nudge still left the 9B (thinking off on quick turns)
saving on 5 of 8 such messages -- 0 of 3 for "let's keep every prompt under 200 words", the
user's own case. The forced choice got 8 of 9 right in ~1-3 s (measured 2026-09-25). It runs
after the reply has streamed, so it adds nothing the user waits for; the single llama-server
slot's conversation cache comes back from the RAM prompt cache (--cache-ram, on by default;
the next turn measured 3.0 s vs 2.5 s without a capture before it).

It is shown only the new message, not the saved memories: with them listed -- even marked
"already saved, never repeat" -- the 9B copied a listed one instead of wording the new fact
("keep your answers to two sentences" came back as the saved Python-examples preference).
Repeats are caught by ``remember``'s own duplicate check instead.
"""
import asyncio
import logging
from typing import Any, Optional

from app.agent.tools.memory import remember
from app.agent.tools.registry import execute_tool

logger = logging.getLogger("jarvis.agent.memory_capture")

NOTHING_TO_REMEMBER = {
    "type": "function",
    "function": {
        "name": "nothing_to_remember",
        "description": "The message holds nothing that will matter in a later conversation.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

_SYSTEM = (
    "You maintain Jarvis's long-term memory. Decide whether the user's message below states something "
    "that will still matter in later conversations: a standing rule or preference, a fact about the user "
    "or the project, a decision, or an open task. If so, call remember with it as one plain sentence that "
    "makes sense on its own later; otherwise call nothing_to_remember. Questions, one-off requests about the "
    "current task, thanks and chit-chat are nothing_to_remember."
)


def _scope_note(in_project: bool) -> str:
    if in_project:
        return ("The conversation is in a project workspace: rules about this project's work are kind=project "
                "(or decision/task); facts about the user and how they like things in general are "
                "about_user/preference/workflow.")
    return ("The conversation is not in a project, so only about_user, preference or workflow can be saved; "
            "if it's only about some project, call nothing_to_remember.")


async def capture_memory(
    provider: Any,
    model: str,
    user_message: str,
    tool_context: dict[str, Any],
) -> Optional[str]:
    """Ask the model to save or skip this message; returns the remember result, or None when it
    skipped or anything failed (a missed memory is not worth an error)."""
    system = _SYSTEM + "\n\n" + _scope_note(bool(tool_context.get("project_id")))
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": f"User's message:\n{user_message}"},
    ]
    try:
        response = await provider.chat(
            messages=messages, model=model, tools=[remember, NOTHING_TO_REMEMBER],
            temperature=0.2, thinking=False, tool_choice="required", timeout=60,
        )
    except Exception as e:
        logger.warning("Memory capture request failed: %s", e)
        return None
    calls = (response.get("message") or {}).get("tool_calls") or []
    call = next((c for c in calls if (c.get("function") or {}).get("name") == "remember"), None)
    if call is None:
        logger.info("Memory capture: nothing to remember in %r", user_message[:80])
        return None
    args = (call.get("function") or {}).get("arguments") or {}
    if not isinstance(args, dict):
        return None
    result = await asyncio.to_thread(execute_tool, "remember", args, tool_context)
    logger.info("Memory capture: %s (%r)", result, args.get("content"))
    return result
