"""The thread keeper (PLAN 4.18 phase 2): the Freeform thread's background housekeeping.

Every minute it looks at the thread and, only when the main model is loaded and nothing else is
running, does the work no turn should wait for:

1. **Closes** an open episode that has been quiet for the gap (3 h), so its record can be written
   before the user comes back -- the next episode's digest then has it.
2. **Writes episode records** (title, summary, decisions, open items) for closed episodes, oldest
   first. If main isn't loaded (Game mode, freed VRAM) they wait for the next time it is.
3. **Folds ahead**: an open episode past 85% of its budget folds its oldest half *between turns*,
   with a real summary of what was folded -- the mid-turn fold in ``thread.py`` is only the safety
   net and its note just says what was folded.
4. **Refreshes a digest** that was built while the records it names were still pending: one extra
   re-prefill for that episode, once, instead of an episode-long "summary pending".

The writing itself is one short side request with a forced tool call (``episode_record``), the way
``memory_capture`` makes the model decide: the model words the summary, the schema keeps it in
four fields. Main, not the fast 2B: the old compactor's fast-model summary squeezed 12,760 tokens
of conversation into 70.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Callable, Optional

from app.memory.thread import THREAD_SESSION_ID, ThreadEpisodes, build_digest

logger = logging.getLogger("jarvis.memory.thread_keeper")

QUIET_SECONDS = 60          # the thread must be this quiet before the keeper uses the model
FOLD_AHEAD_RATIO = 0.85     # fold between turns once an episode passes this share of its budget
MAX_RECORDS_PER_TICK = 3
TOOL_RESULT_CHARS = 400     # tool output is evidence, not conversation: clipped in transcripts
TRANSCRIPT_CHARS = 60_000   # ~15k tokens: the newest part of a very long episode

EPISODE_RECORD_TOOL = {
    "type": "function",
    "function": {
        "name": "episode_record",
        "description": "Record what this stretch of conversation between the user and Jarvis was about.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string",
                          "description": "3-7 words naming the topic(s), e.g. 'GPU temps and Game mode'."},
                "summary": {"type": "string",
                            "description": "2-4 plain sentences: what the user wanted and what happened. "
                                           "Keep exact names, numbers, file names and commands."},
                "decisions": {"type": "string",
                              "description": "What was decided or settled, as short phrases; '' if nothing."},
                "open_items": {"type": "string",
                               "description": "What is still to do or was promised for later; '' if nothing."},
            },
            "required": ["title", "summary", "decisions", "open_items"],
        },
    },
}

_SYSTEM = (
    "You write the record of one stretch of conversation between a user and their assistant, Jarvis, "
    "so it can be recalled later. Call episode_record. Only state what the transcript shows -- never "
    "invent outcomes, numbers or promises. Write about the user in the third person ('the user')."
)


def transcript(rows: list[dict[str, Any]], prior_note: Optional[str] = None) -> str:
    lines: list[str] = []
    if prior_note:
        lines.append(f"(Before this part: {prior_note.strip()})")
    for row in rows:
        role = row.get("role")
        content = (row.get("content") or "").strip()
        at = datetime.fromtimestamp(row["created_at"]).strftime("%H:%M") if row.get("created_at") else ""
        if role == "user":
            lines.append(f"User {at}: {content}")
        elif role == "assistant" and content:
            lines.append(f"Jarvis: {content}")
        elif role == "tool":
            clipped = content if len(content) <= TOOL_RESULT_CHARS else content[:TOOL_RESULT_CHARS] + " ..."
            lines.append(f"[tool {row.get('name') or ''}: {clipped}]")
    text = "\n".join(lines)
    return text if len(text) <= TRANSCRIPT_CHARS else "(earlier part cut)\n" + text[-TRANSCRIPT_CHARS:]


async def write_record(provider: Any, rows: list[dict[str, Any]], prior_note: Optional[str] = None,
                       model: str = "main") -> Optional[dict[str, str]]:
    """The four record fields from the main model, or None when it failed (try again later)."""
    if not rows:
        return {"title": "Empty episode", "summary": "", "decisions": "", "open_items": ""}
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": "Transcript:\n" + transcript(rows, prior_note)},
    ]
    try:
        response = await provider.chat(messages=messages, model=model, tools=[EPISODE_RECORD_TOOL],
                                       temperature=0.2, thinking=False, tool_choice="required", timeout=180)
    except Exception as e:
        logger.warning("Episode record request failed: %s", e)
        return None
    calls = (response.get("message") or {}).get("tool_calls") or []
    call = next((c for c in calls if (c.get("function") or {}).get("name") == "episode_record"), None)
    args = ((call or {}).get("function") or {}).get("arguments")
    if not isinstance(args, dict) or not str(args.get("title") or "").strip():
        logger.warning("Episode record: no usable call in the reply")
        return None
    return {k: _field(args.get(k)) for k in ("title", "summary", "decisions", "open_items")}


# Ornith filled an empty field with "None noted." on the first live record (2026-10-20).
_EMPTY = {"", "none", "none noted", "nothing", "nothing noted", "n/a", "na", "-", "no", "none yet"}


def _field(value: Any) -> str:
    text = " ".join(str(value or "").split())
    return "" if text.lower().rstrip(".!") in _EMPTY else text


def _sentence(text: str) -> str:
    text = text.strip()
    return text if text.endswith((".", "!", "?")) else text + "."


def fold_note_from(record: dict[str, str]) -> str:
    parts = [f"[Earlier in this episode (folded): {_sentence(record['summary'])}"]
    if record.get("decisions"):
        parts.append(f"Decided: {_sentence(record['decisions'])}")
    if record.get("open_items"):
        parts.append(f"Open: {_sentence(record['open_items'])}")
    return " ".join(parts) + "]"


class ThreadKeeper:
    def __init__(self, store: Any, provider_factory: Callable[[], Any],
                 can_use_model: Callable[[], bool], session_id: str = THREAD_SESSION_ID,
                 episodes: Optional[ThreadEpisodes] = None) -> None:
        self.store = store
        self.provider_factory = provider_factory
        self.can_use_model = can_use_model
        self.session_id = session_id
        self.episodes = episodes or ThreadEpisodes(store)
        self._lock = asyncio.Lock()

    async def tick(self, now: Optional[float] = None) -> list[str]:
        """One pass; returns what it did (for the log and the tests)."""
        if self._lock.locked():
            return []
        async with self._lock:
            now = now or time.time()
            done: list[str] = []
            last = self.store.last_message_meta(self.session_id)
            open_ep = self.store.open_episode(self.session_id)

            # 1. close an episode that went quiet (no model needed)
            if open_ep is not None:
                last_activity = max(open_ep["started_at"], (last or {}).get("created_at") or 0.0)
                if now - last_activity >= self.episodes.gap_seconds:
                    ended = max(last_activity, open_ep["started_at"])
                    self.store.update_episode(open_ep["id"], ended_at=ended, record_status="pending")
                    done.append(f"closed {open_ep['id']}")
                    open_ep = None

            if last is not None and now - (last.get("created_at") or 0.0) < QUIET_SECONDS:
                return done
            if not self.can_use_model():
                return done
            provider = self.provider_factory()

            # 2. records for closed episodes, oldest first
            episodes = self.store.list_episodes(self.session_id)  # newest first
            pending = [e for e in reversed(episodes) if e["record_status"] == "pending"][:MAX_RECORDS_PER_TICK]
            for ep in pending:
                rows = self.store.get_message_rows(self.session_id, after_id=ep["start_after_message_id"],
                                                   upto_id=self._end_id(ep, episodes))
                if ep.get("fold_upto_message_id"):
                    rows = [r for r in rows if r["id"] > ep["fold_upto_message_id"]]
                record = await write_record(provider, rows, prior_note=ep.get("fold_note"))
                if record is None:
                    return done
                self.store.update_episode(ep["id"], record_status="done", **record)
                done.append(f"recorded {ep['id']}: {record['title']}")

            if open_ep is None:
                return done
            # 3. fold ahead, between turns, with a real summary
            rows = self.episodes.visible_rows(self.session_id, open_ep)
            if self.episodes.needs_fold(rows, FOLD_AHEAD_RATIO):
                keep_from = self.episodes.fold_point(rows)
                if 0 < keep_from < len(rows):
                    record = await write_record(provider, rows[:keep_from], prior_note=open_ep.get("fold_note"))
                    if record is not None:
                        open_ep, _ = self.episodes.fold(open_ep, rows, note=fold_note_from(record))
                        done.append(f"folded {open_ep['id']}")
            # 4. a digest built while its records were pending
            if open_ep.get("digest") and "summary pending" in open_ep["digest"]:
                fresh = build_digest(self.store, self.session_id, open_ep["started_at"], exclude_id=open_ep["id"])
                if fresh != open_ep["digest"] and "summary pending" not in fresh:
                    self.store.update_episode(open_ep["id"], digest=fresh)
                    done.append(f"digest {open_ep['id']}")
            return done

    def _end_id(self, ep: dict[str, Any], episodes: list[dict[str, Any]]) -> Optional[int]:
        """The last message id of a closed episode: everything before the next episode began."""
        later = [e for e in episodes if e["id"] > ep["id"]]
        return min(e["start_after_message_id"] for e in later) if later else None


async def keeper_loop(keeper: ThreadKeeper, interval_s: float = 60.0) -> None:
    while True:
        try:
            for line in await keeper.tick():
                logger.info("Thread keeper: %s", line)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Thread keeper pass failed: %s", exc)
        await asyncio.sleep(interval_s)
