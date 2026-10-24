"""
A thinking block that has started repeating itself (2026-10-24, "search for a document project u").

Ornith thought for 87 s, ran into the 8,192-token reply cap without a word of answer (94 tok/s against
~27 normally: the speculative draft accepts nearly everything when the text repeats), and the
"you stopped before acting" nudge sent it straight back into the same loop. ``LoopWatch`` reads the
reasoning as it streams and says when its newest stretch already occurs twice earlier; the
orchestrator then stops the round and asks again with thinking off.
"""

from __future__ import annotations

WINDOW = 160        # the newest stretch looked for
MIN_CHARS = 2000    # nothing under this is called a loop
CHECK_EVERY = 400   # new characters between checks
REPEATS = 2         # earlier occurrences that make it a loop


class LoopWatch:
    def __init__(self) -> None:
        self._parts: list[str] = []
        self._chars = 0
        self._checked_at = 0
        self.looping = False

    def feed(self, delta: str) -> bool:
        """Add streamed thinking; True once it is looping."""
        if self.looping or not delta:
            return self.looping
        self._parts.append(delta)
        self._chars += len(delta)
        if self._chars < MIN_CHARS or self._chars - self._checked_at < CHECK_EVERY:
            return False
        self._checked_at = self._chars
        text = "".join(self._parts)
        self._parts = [text]
        tail = " ".join(text[-WINDOW:].split())
        if len(tail) < WINDOW // 2:
            return False
        earlier = " ".join(text[:-WINDOW].split())
        self.looping = earlier.count(tail) >= REPEATS
        return self.looping


def capped(timings: dict | None, max_tokens: int) -> bool:
    """The round ran into the reply cap (``max_tokens``) -- with no answer, the thinking never ended."""
    if not timings or max_tokens <= 0:
        return False
    return int(timings.get("predicted_n") or 0) >= max_tokens - 1
