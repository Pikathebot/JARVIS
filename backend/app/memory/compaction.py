"""
Compaction the way Claude Code does it (PLAN 4.25), for both spaces.

After a turn whose request passed ~80% of the model's window (the server's own count: cached +
new prompt tokens + the reply), the conversation so far is summarized -- in the background, so no
turn waits -- into one structured summary: what the user wants, facts and decisions, what was
done, errors and fixes, what's still open, where it left off. From then on the model sees that
summary plus the last few exchanges verbatim; everything older stays in the database and on the
user's screen. ``/compact [focus]`` does the same on request, and waits for it.

Where it lives:
- **Freeform thread:** the open episode's fold (``fold_upto_message_id`` + ``fold_note``), so the
  thread's own context path (``ThreadEpisodes.context``) picks it up unchanged.
- **Workspace sessions:** a ``compaction_events`` row, strategy ``summary``, with the summary and the
  last message it covers (no schema change); ``workspace_history`` turns it into the history rows
  and the preamble the context builder takes.
- **Both:** a marker message (role ``system``, name ``compaction``, ``is_summary``) after the last
  message -- the divider in the chat. It is never replayed to the model (``_replay_units``).

The summary rides as the history's preamble, on the first kept user message, so it is
byte-identical on every turn until the next compaction (the KV-cache prefix contract).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Any, Optional

from app.config import settings

logger = logging.getLogger("jarvis.memory.compaction")

STRATEGY = "summary"
MARKER_NAME = "compaction"
COMPACT_AT = 0.8      # share of the window a turn's request may reach before the next compaction
KEEP_SHARE = 0.2      # the newest exchanges kept verbatim: about this share of the window
SUMMARY_MAX_TOKENS = 1500

SUMMARY_PROMPT = """You are compacting a long conversation between a user and their assistant, Jarvis, so it can continue in a fresh context. Write a summary Jarvis will read instead of the conversation below. Be specific: names, numbers, file paths, dates, titles, links -- whatever was established. No preamble.

Use exactly these sections, each a few short bullet points (write "None" where a section is empty):

## What the user wants
Their requests in this conversation and the intent behind them, in order; the latest one last.
## Facts and decisions
What was established or agreed, and preferences the user stated.
## What was done
Tools that ran and what they changed or found (notes, tasks, events, reminders, files, searches).
## Errors and fixes
What went wrong and how it was resolved, or that it wasn't.
## Still open
Questions not yet answered, things promised or left to do.
## Where it left off
What was being worked on at the very end, in one or two sentences.
{focus}
{prior}--- Conversation ---
{transcript}
--- End of conversation ---"""

# Background runs and notices for the next turn of each session, in this process.
_running: set[str] = set()
_notices: dict[str, dict[str, Any]] = {}


def _clock(ts: Optional[float]) -> str:
    return datetime.fromtimestamp(ts).strftime("%d %b %H:%M") if ts else ""


def preamble(summary: str, at: Optional[float] = None) -> str:
    """What the model reads in place of the compacted part."""
    when = f" at {_clock(at)}" if at else ""
    return (f"[Summary of this conversation up to here (compacted{when}; the full history is still on "
            f"the user's screen):\n{summary.strip()}\n]")


def window_for(model: str) -> int:
    return settings.llama_ctx_size_fast if model == "fast" else settings.llama_ctx_size_main


def should_compact(peak_tokens: Optional[int], window: int) -> bool:
    return bool(peak_tokens) and peak_tokens >= COMPACT_AT * window


# ---- the summary ------------------------------------------------------------------------

def conversation_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The rows that are conversation: markers and stored summaries out."""
    return [r for r in rows if not r.get("is_summary") and r.get("role") != "system"]


def keep_point(rows: list[dict[str, Any]], keep_tokens: int, counter: Any) -> int:
    """Index of the first row kept verbatim: the newest exchanges worth about ``keep_tokens``,
    starting at a user message (a call and its result are never split), always the last exchange."""
    from app.memory.context_manager import _replay_units

    keep_from, kept = len(rows), 0
    for i in range(len(rows) - 1, -1, -1):
        clean = {k: v for k, v in rows[i].items() if k not in ("id", "created_at")}
        kept += sum(counter.count_messages(u) for u in _replay_units([clean]))
        if rows[i].get("role") == "user":
            if keep_from < len(rows) and kept > keep_tokens:
                break
            keep_from = i
    return keep_from


async def write_summary(provider: Any, rows: list[dict[str, Any]], prior: Optional[str] = None,
                        focus: Optional[str] = None, model: str = "main") -> str:
    from app.memory.thread_keeper import transcript

    prompt = SUMMARY_PROMPT.format(
        focus=f"\nThe user asked the summary to focus on: {focus.strip()}\n" if focus and focus.strip() else "",
        prior=f"--- Summary of the part before this ---\n{prior.strip()}\n\n" if prior else "",
        transcript=transcript(rows),
    )
    response = await provider.chat(model=model, messages=[{"role": "user", "content": prompt}],
                                   thinking=False, temperature=0.3, timeout=300)
    text = ((response or {}).get("message") or {}).get("content") or ""
    from app.agent.llamacpp_provider import strip_thinking_tags

    return strip_thinking_tags(text)[0].strip()


# ---- where it is stored -----------------------------------------------------------------

def latest(store: Any, session_id: str) -> Optional[dict[str, Any]]:
    """A Workspace session's last compaction: ``{"summary", "upto_message_id", "at"}``."""
    try:
        events = store.get_compaction_events(session_id)
    except Exception:
        return None
    for ev in reversed(events or []):
        if ev.get("strategy") != STRATEGY:
            continue
        try:
            details = json.loads(ev.get("details") or "{}")
        except (TypeError, ValueError):
            continue
        if details.get("summary") and details.get("upto_message_id"):
            return {"summary": details["summary"], "upto_message_id": int(details["upto_message_id"]),
                    "at": ev.get("created_at")}
    return None


def workspace_history(store: Any, session_id: str) -> Optional[tuple[list[dict[str, Any]], str]]:
    """(the rows after the last compaction, its preamble), or None when the session has none."""
    last = latest(store, session_id)
    if last is None or not hasattr(store, "get_message_rows"):
        return None
    return store.get_message_rows(session_id, after_id=last["upto_message_id"]), preamble(last["summary"], last["at"])


# ---- doing it ---------------------------------------------------------------------------

async def compact(store: Any, provider: Any, session_id: str, model: str = "main",
                  focus: Optional[str] = None, manual: bool = False, counter: Any = None) -> Optional[dict[str, Any]]:
    """Compact the session now. The notice (``{"summary", "at", "manual"}``) or None when there was
    nothing to fold (one exchange) or the summary failed."""
    from app.memory.thread import ThreadEpisodes, is_thread
    from app.memory.token_counter import TokenCounter

    if session_id in _running:
        return None
    _running.add(session_id)
    try:
        counter = counter or TokenCounter()
        window = window_for(model)
        # manual: keep only the last exchange, as /compact in Claude Code keeps nothing
        keep_tokens = 0 if manual else int(KEEP_SHARE * window)
        thread = is_thread(session_id)
        if thread:
            episodes = ThreadEpisodes(store, counter)
            episode = store.open_episode(session_id)
            if episode is None:
                return None
            rows = conversation_rows(episodes.visible_rows(session_id, episode))
            prior = episode.get("fold_note")
        else:
            last = latest(store, session_id)
            rows = conversation_rows(store.get_message_rows(session_id, after_id=(last or {}).get("upto_message_id") or 0))
            prior = (last or {}).get("summary")
        keep_from = keep_point(rows, keep_tokens, counter)
        if keep_from <= 0:
            logger.info("Nothing to compact in %s: one exchange fills it.", session_id)
            return None
        folded = rows[:keep_from]
        started = time.time()
        summary = await write_summary(provider, folded, prior=prior, focus=focus, model=model)
        if not summary:
            logger.warning("Compaction of %s gave an empty summary; nothing changed.", session_id)
            return None
        now = time.time()
        upto = folded[-1]["id"]
        if thread:
            fields = {"fold_upto_message_id": upto, "fold_note": preamble(summary, now)}
            store.update_episode(episode["id"], **fields)
        else:
            store.record_compaction(session_id, STRATEGY, tokens_before=0, tokens_after=0,
                                    details=json.dumps({"summary": summary, "upto_message_id": upto,
                                                        "manual": manual, "focus": focus or ""}))
        store.append_message(session_id, role="system", content=summary, name=MARKER_NAME, is_summary=True)
        logger.info("Compacted %s: %d rows into a %d-char summary in %.1f s (%s).", session_id, len(folded),
                    len(summary), now - started, "asked" if manual else "auto")
        return {"summary": summary, "at": now, "manual": manual}
    except Exception as e:
        logger.warning("Compaction of %s failed: %s", session_id, e)
        return None
    finally:
        _running.discard(session_id)


def schedule(store: Any, provider: Any, session_id: str, model: str, counter: Any = None) -> None:
    """Compact in the background after a turn; the next turn of the session announces it."""
    if session_id in _running:
        return

    async def run() -> None:
        notice = await compact(store, provider, session_id, model=model, counter=counter)
        if notice:
            _notices[session_id] = notice

    try:
        asyncio.get_running_loop().create_task(run())
    except RuntimeError:
        pass


def take_notice(session_id: str) -> Optional[dict[str, Any]]:
    return _notices.pop(session_id, None)


def parse_command(message: str) -> Optional[str]:
    """``/compact`` -> "", ``/compact the calendar bits`` -> "the calendar bits", else None."""
    text = (message or "").strip()
    if text.lower() == "/compact" or text.lower().startswith("/compact "):
        return text[len("/compact"):].strip()
    return None
