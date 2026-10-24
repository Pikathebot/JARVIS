"""
Keeping one request inside the model's window (PLAN 4.23c).

``ContextManager.build_context`` budgets the history before a turn, but two things it couldn't see
filled the window anyway: the tool schemas, which the chat template renders into the head of every
request (~20k characters, 5-6k tokens), and the tool results a turn gathers *after* the budget was
drawn. 2026-10-24: a turn budgeted at 6,531 tokens reached the server as 16,575.

``tools_tokens`` prices the schemas so the budget can reserve them. ``fit_messages`` runs before
every request of a turn: when messages + tools would pass the window less the reply reserve, it
cuts the biggest tool results down first (keeping their start, saying how much was cut), and only
if that isn't enough drops the oldest whole exchanges of the history -- never the system message,
the current request or a call apart from its result.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from app.memory.token_counter import TokenCounter

logger = logging.getLogger("jarvis.memory.turn_fit")

# A tool result is never cut below this: enough to say what it was and that it was cut.
MIN_RESULT_CHARS = 1200
CUT_NOTE = "\n[... {n:,} more chars cut to fit the context window; call the tool again with a narrower request for them]"

_counter = TokenCounter()


def tools_tokens(tools: Optional[list[Any]], counter: Optional[TokenCounter] = None) -> int:
    """Tokens the tool schemas cost in the head (one JSON object per line, as the template renders them)."""
    if not tools:
        return 0
    from app.agent.tool_schema import convert_tool_to_openai_schema

    schemas = [s for s in (t if isinstance(t, dict) else convert_tool_to_openai_schema(t) for t in tools) if s]
    rendered = "\n".join(json.dumps(s, ensure_ascii=False) for s in schemas)
    # + the template's framing around the tool list
    return (counter or _counter).count(rendered) + 40


def _units(messages: list[dict[str, Any]]) -> list[list[int]]:
    """Message indexes grouped into exchanges: each starts at a user message (the system head and
    anything before the first user message form unit 0)."""
    units: list[list[int]] = [[]]
    for i, m in enumerate(messages):
        if m.get("role") == "user" and units[-1]:
            units.append([])
        units[-1].append(i)
    return units


def fit_messages(
    messages: list[dict[str, Any]],
    window: int,
    tools_cost: int = 0,
    reserve: int = 2048,
    counter: Optional[TokenCounter] = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """``messages`` cut to fit ``window`` - ``reserve`` with the tools priced in, and what was done
    ("cut 2 tool results", "dropped 3 earlier exchanges"); unchanged (the same list) when it fits."""
    counter = counter or _counter
    limit = window - reserve - tools_cost
    if counter.count_messages(messages) <= limit:
        return messages, []

    fitted = [dict(m) for m in messages]
    done: list[str] = []

    # 1. The biggest tool results first, each down to half until they fit or reach the floor.
    cut: set[int] = set()
    while counter.count_messages(fitted) > limit:
        tool_ix = [i for i, m in enumerate(fitted)
                   if m.get("role") == "tool" and len(m.get("content") or "") > MIN_RESULT_CHARS * 2]
        if not tool_ix:
            break
        i = max(tool_ix, key=lambda j: len(fitted[j]["content"]))
        text = fitted[i]["content"]
        keep = max(MIN_RESULT_CHARS, len(text) // 2)
        fitted[i]["content"] = text[:keep] + CUT_NOTE.format(n=len(text) - keep)
        cut.add(i)
    if cut:
        done.append(f"cut {len(cut)} tool result{'s' if len(cut) != 1 else ''}")

    # 2. Then the oldest exchanges, keeping the head and the last two exchanges.
    dropped = 0
    units = _units(fitted)
    # the last two units: a nudge the loop sent rides as its own user message after the request
    while counter.count_messages(fitted) > limit and len(units) > 3:
        gone = set(units.pop(1))
        fitted = [m for j, m in enumerate(fitted) if j not in gone]
        units = _units(fitted)
        dropped += 1
    if dropped:
        done.append(f"dropped {dropped} earlier exchange{'s' if dropped != 1 else ''}")

    if counter.count_messages(fitted) > limit:
        done.append("still over")
    logger.info("Request fitted to the %d-token window (tools %d): %s.", window, tools_cost, ", ".join(done))
    return fitted, done
