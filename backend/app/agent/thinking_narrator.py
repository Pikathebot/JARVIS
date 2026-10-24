"""
A few words on what the model is doing while it thinks (PLAN 4.23).

While a turn's reasoning streams, every few seconds the latest stretch of it goes to llama-server's
second slot with a one-line request ("say in a few words what this is doing"); the answer is shown
above the thinking ("Checking the calendar for Friday"). The turn itself is pinned to slot 0
(``llamacpp_provider._main_slot``), so its cached prefix is never touched, and these requests ask
for no cache (``cache_prompt: false``): nothing of them is kept between calls.

Best effort throughout: a failed, slow or empty narration is simply not shown.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Optional

import httpx

from app.config import settings

logger = logging.getLogger("jarvis.agent.thinking_narrator")

NARRATOR_SLOT = 1
# The stretch of thinking it reads: enough for the current step, small enough to prefill in ~0.1 s.
TAIL_CHARS = 1200
# Thinking it waits for before the first line, and new thinking it needs before the next one.
FIRST_AFTER_S = 1.5
MIN_NEW_CHARS = 120
MAX_LINE_CHARS = 60
REQUEST_TIMEOUT_S = 8.0

_PROMPT = (
    "Below is the latest part of an assistant's private reasoning. In 3 to 7 words, say what it is "
    "doing right now, as a phrase starting with a verb ending in -ing, like \"Checking the calendar "
    "for Friday\" or \"Weighing two ways to word the reply\". No quotes, no full stop, nothing else.\n\n"
    "<reasoning>\n{tail}\n</reasoning>"
)

_QUOTES = "\"'`“”‘’*"


def clean_line(text: str) -> str:
    """The model's reply as one short line, or "" when it isn't one."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    line = next((part.strip() for part in text.splitlines() if part.strip()), "")
    line = line.strip(_QUOTES).rstrip(".!:;,").strip(_QUOTES).strip()
    if not line or "<" in line or len(line) > MAX_LINE_CHARS * 2:
        return ""
    if len(line) > MAX_LINE_CHARS:
        line = line[:MAX_LINE_CHARS].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return line[0].upper() + line[1:]


class ThinkingNarrator:
    """Feed it the reasoning as it streams; ``take`` returns a new line when one is ready."""

    def __init__(self, base_url: str, model: str, interval_s: Optional[float] = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.interval_s = settings.thinking_narrator_interval_s if interval_s is None else interval_s
        self._text: list[str] = []
        self._chars = 0
        self._chars_at_last = 0
        self._started_at: Optional[float] = None
        self._last_request_at = 0.0
        self._task: Optional[asyncio.Task[str]] = None
        self._last_line = ""
        self._stopped = False

    def feed(self, delta: str) -> None:
        if self._stopped or not delta:
            return
        now = time.monotonic()
        if self._started_at is None:
            self._started_at = now
        self._text.append(delta)
        self._chars += len(delta)
        if self._task is not None:
            return
        if now - self._started_at < FIRST_AFTER_S or now - self._last_request_at < self.interval_s:
            return
        if self._chars - self._chars_at_last < MIN_NEW_CHARS:
            return
        tail = "".join(self._text)[-TAIL_CHARS:]
        self._chars_at_last = self._chars
        self._last_request_at = now
        self._task = asyncio.create_task(self._narrate(tail))

    def take(self) -> Optional[str]:
        """A finished line not shown yet (and different from the last one), else None."""
        task = self._task
        if task is None or not task.done():
            return None
        self._task = None
        if task.cancelled() or task.exception() is not None:
            return None
        line = task.result()
        if not line or line == self._last_line or self._stopped:
            return None
        self._last_line = line
        return line

    def stop(self) -> None:
        """The thinking ended (the answer or a call began): nothing more is asked or shown."""
        self._stopped = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    async def _narrate(self, tail: str) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": _PROMPT.format(tail=tail)}],
            "stream": False,
            "max_tokens": 20,
            "temperature": 0.3,
            "top_p": 0.8,
            "id_slot": NARRATOR_SLOT,
            "cache_prompt": False,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S) as client:
                resp = await client.post(f"{self.base_url}/v1/chat/completions", json=payload)
            if resp.status_code != 200:
                logger.debug("Narrator request refused (%s): %s", resp.status_code, resp.text[:200])
                return ""
            msg = (resp.json().get("choices") or [{}])[0].get("message") or {}
            return clean_line(msg.get("content") or "")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("Narrator request failed: %s", e)
            return ""


def narrator_for(provider: Any, model: str, thinking: Optional[bool]) -> Optional[ThinkingNarrator]:
    """A narrator for this turn, or None: off, one slot only, thinking off, or not llama-server."""
    if not settings.thinking_narrator_enabled or settings.llama_parallel < 2 or thinking is False:
        return None
    base_url = getattr(provider, "base_url", None)
    if getattr(provider, "name", "") != "llama_cpp" or not isinstance(base_url, str):
        return None
    return ThinkingNarrator(base_url, model)
