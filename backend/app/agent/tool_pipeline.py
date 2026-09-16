"""
The guarded path from "the model asked for a tool" to "here is what the model sees back".

Tools used to be called straight from the async agent loop: a native tool is a plain
synchronous function, so a slow one (a build, a hung command, a web page that never answers)
did not just stall the turn, it stalled the whole event loop -- SSE, the awareness monitor,
voice. And whatever it returned went to the model verbatim, however large.

This pipeline runs every tool through the same stages, native or MCP:

    execute   -- native tools on a worker thread, MCP tools awaited; both under a per-tool
                 timeout, so the loop is never blocked and never waits forever
    post      -- ordered hooks may replace or block the result before the model sees it
                 (the built-in one caps oversized output, keeping the head and the tail)
    outcome   -- one immutable record: text, duration, whether it timed out or errored

Timeouts are advisory for native tools: Python cannot kill a thread, so a timed-out tool keeps
running in the background until it finishes on its own, but the model gets its error now and
the turn moves on. Register longer limits for tools that legitimately take a while.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from app.config import settings

logger = logging.getLogger("jarvis.agent.tool_pipeline")

# Per-tool limits in seconds; anything else uses settings.tool_default_timeout_seconds.
TOOL_TIMEOUTS: dict[str, float] = {
    "execute_command": 180.0,
    "run_command": 180.0,
    "unreal.build": 900.0,
    "web.search": 45.0,
    "web.extract": 60.0,
    "web_search": 45.0,
    "fetch_url": 60.0,
}


@dataclass
class ToolOutcome:
    tool: str
    args: dict[str, Any]
    result: str
    duration_ms: int = 0
    timed_out: bool = False
    error: bool = False
    truncated: bool = False
    hooks_applied: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.error or self.timed_out)

    def to_event(self) -> dict[str, Any]:
        """The ``tool_end`` payload: what the client shows on the tool card."""
        return {
            "tool": self.tool,
            "args": self.args,
            "status": "success" if self.ok else ("timeout" if self.timed_out else "error"),
            "result": self.result[:2000],
            "duration_ms": self.duration_ms,
            "timed_out": self.timed_out,
            "truncated": self.truncated,
        }


# A post-execute hook sees the outcome and returns replacement text, or None to leave it.
PostHook = Callable[[ToolOutcome], Optional[str]]


def truncate_oversized(outcome: ToolOutcome, max_chars: Optional[int] = None) -> Optional[str]:
    """
    Built-in post hook: cap a result at ``max_chars`` keeping the first and last parts, since
    both ends of a log or listing tend to carry the useful bits (the command, the final error).
    """
    limit = max_chars if max_chars is not None else settings.tool_result_max_chars
    text = outcome.result
    if limit <= 0 or len(text) <= limit:
        return None
    head = int(limit * 0.7)
    tail = limit - head
    omitted = len(text) - limit
    outcome.truncated = True
    return (
        text[:head]
        + f"\n\n[... {omitted:,} characters omitted from the middle of this {outcome.tool} result ...]\n\n"
        + text[-tail:]
    )


class ToolPipeline:
    def __init__(
        self,
        default_timeout: Optional[float] = None,
        timeouts: Optional[dict[str, float]] = None,
        post_hooks: Optional[list[tuple[str, PostHook]]] = None,
    ):
        self.default_timeout = default_timeout if default_timeout is not None else settings.tool_default_timeout_seconds
        self.timeouts = dict(TOOL_TIMEOUTS if timeouts is None else timeouts)
        self.post_hooks: list[tuple[str, PostHook]] = (
            list(post_hooks) if post_hooks is not None else [("truncate_oversized", truncate_oversized)]
        )

    def timeout_for(self, tool: str) -> float:
        return self.timeouts.get(tool, self.default_timeout)

    def add_post_hook(self, name: str, hook: PostHook) -> None:
        self.post_hooks.append((name, hook))

    async def run(
        self,
        tool: str,
        args: dict[str, Any],
        native: Optional[Callable[[], str]] = None,
        coroutine: Optional[Callable[[], Awaitable[str]]] = None,
    ) -> ToolOutcome:
        """
        Execute one tool. Pass ``native`` (a zero-arg sync callable) for a built-in tool, or
        ``coroutine`` (a zero-arg async callable) for an MCP tool. Never raises: every failure
        mode becomes a ToolOutcome the model can read.
        """
        limit = self.timeout_for(tool)
        started = time.time()
        outcome = ToolOutcome(tool=tool, args=args, result="")
        try:
            if coroutine is not None:
                result = await asyncio.wait_for(coroutine(), timeout=limit)
            elif native is not None:
                result = await asyncio.wait_for(asyncio.to_thread(native), timeout=limit)
            else:
                raise ValueError("ToolPipeline.run needs either native or coroutine")
            outcome.result = str(result)
        except asyncio.TimeoutError:
            outcome.timed_out = True
            outcome.result = (
                f"Error: tool '{tool}' timed out after {limit:.0f} seconds. "
                "It may still be running in the background; do not repeat the same call."
            )
            logger.warning("Tool '%s' timed out after %.0fs", tool, limit)
        except Exception as e:
            outcome.error = True
            outcome.result = f"Error executing tool '{tool}': {e}"
            logger.error("Tool '%s' raised: %s", tool, e)
        outcome.duration_ms = int((time.time() - started) * 1000)

        # Native tools report their own failures as "Error..." strings rather than raising.
        if not outcome.timed_out and outcome.result.startswith("Error"):
            outcome.error = True

        for name, hook in self.post_hooks:
            try:
                replacement = hook(outcome)
            except Exception as e:
                logger.warning("Post-execute hook %s failed on %s: %s", name, tool, e)
                continue
            if replacement is not None:
                outcome.result = replacement
                outcome.hooks_applied.append(name)

        logger.info(
            "Tool '%s' finished in %dms (%s%s)", tool, outcome.duration_ms,
            "timeout" if outcome.timed_out else ("error" if outcome.error else "ok"),
            ", truncated" if outcome.truncated else "",
        )
        return outcome


_pipeline: Optional[ToolPipeline] = None


def get_tool_pipeline() -> ToolPipeline:
    global _pipeline
    if _pipeline is None:
        _pipeline = ToolPipeline()
    return _pipeline
