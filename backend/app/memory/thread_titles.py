"""Names for the open topic of the Freeform thread (PLAN 4.18), written by the model as it goes.

The user asked (2026-10-20) for topics to be named by the AI itself: first from the opening
exchange, then renamed as the conversation moves on, and finally from the summary. The last step
is the thread keeper's record (title + summary, written when the episode closes); this module does
the first two, in the background right after a reply:

* **after the topic's first reply** -- a name from the first exchange;
* **every ``RETITLE_EVERY`` user turns after that** -- a name for the whole topic so far.

The request is the turn's own context (same head, same tools, same history and message) with the
reply and a naming question appended, so llama-server reuses the cached prefix: about a second of
new tokens, and the next turn still matches the cache up to this turn's message. One slot serves
both, so a message sent meanwhile waits for it -- that is the whole cost.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Optional

logger = logging.getLogger("jarvis.memory.thread_titles")

RETITLE_EVERY = 4     # user turns between renames of the open topic
TITLE_MAX_CHARS = 60
MIN_WORDS_TO_NAME = 4  # what the user has said in the topic before it gets a first name

TITLE_ASK = (
    "(A note from the app, not the user: don't answer it as a message.) In 3-6 words, name what this "
    "topic of our conversation is about, e.g. 'GPU temps and Game mode'. Reply with the name only -- "
    "no quotes, no full stop, no tools."
)

_tasks: set[asyncio.Task] = set()


def due(store: Any, session_id: str, episode_id: int) -> Optional[int]:
    """The message id a new name would reach to, when the open episode is due one; else None."""
    episode = next((e for e in store.list_episodes(session_id, limit=3) if e["id"] == episode_id), None)
    if episode is None or episode["record_status"] != "open":
        return None
    rows = store.get_message_rows(session_id, after_id=episode["start_after_message_id"])
    if not rows or not any(r["role"] == "assistant" and (r.get("content") or "").strip() for r in rows):
        return None
    last_id = rows[-1]["id"]
    if not episode.get("title"):
        # "hey jarvis" alone has nothing to name (Ornith called it "Good evening, sir" on the live
        # probe, 2026-10-20): wait for a real request.
        said = sum(len((r.get("content") or "").split()) for r in rows if r["role"] == "user")
        return last_id if said >= MIN_WORDS_TO_NAME else None
    since = episode.get("title_upto_message_id") or episode["start_after_message_id"]
    turns = sum(1 for r in rows if r["role"] == "user" and r["id"] > since)
    return last_id if turns >= RETITLE_EVERY else None


def clean_title(text: str) -> Optional[str]:
    """The model's reply as a title, or None when it isn't one (a sentence, an answer, empty)."""
    line = next((l for l in (text or "").strip().splitlines() if l.strip()), "")
    line = re.sub(r"^(?:title|name|topic)\s*:\s*", "", line.strip(), flags=re.IGNORECASE)
    line = line.strip(" \"'`*_#").rstrip(".!?;:,").strip()
    words = line.split()
    if not 1 <= len(words) <= 8 or len(line) > TITLE_MAX_CHARS:
        return None
    # Named after the app rather than the talk: "Freeform conversation, no project" (device 2026-10-20).
    if {w.strip(",.-:;").lower() for w in words} <= _GENERIC:
        return None
    return line[0].upper() + line[1:]


_GENERIC = {"freeform", "conversation", "chat", "topic", "new", "general", "no", "project", "discussion",
            "misc", "miscellaneous", "various", "casual", "talk", "and", "a", "the", "with", "jarvis",
            "assistant", "user", "greeting", "greetings", "small", "random", "day", "to", "daily", ""}


async def name_topic(provider: Any, model: str, messages: list[dict[str, Any]], answer: str,
                     tools: Optional[list[Any]], store: Any, session_id: str, episode_id: int,
                     upto_id: int) -> Optional[str]:
    """Asks for the name and stores it; returns it (None when the reply wasn't usable)."""
    try:
        response = await provider.chat(
            messages=messages + [{"role": "assistant", "content": answer}, {"role": "user", "content": TITLE_ASK}],
            model=model, tools=tools or None, temperature=0.2, thinking=False, timeout=60)
    except Exception as e:
        logger.info("Topic name request failed: %s", e)
        return None
    message = response.get("message") or {}
    if message.get("tool_calls"):
        return None
    title = clean_title(message.get("content") or "")
    if title is None:
        logger.info("Topic name unusable: %r", (message.get("content") or "")[:80])
        return None
    episode = next((e for e in store.list_episodes(session_id, limit=3) if e["id"] == episode_id), None)
    if episode is None or episode["record_status"] != "open":
        return None  # closed (its record names it now) or forgotten meanwhile
    store.update_episode(episode_id, title=title, title_upto_message_id=upto_id)
    logger.info("Thread topic %s named: %s", episode_id, title)
    return title


def schedule(provider: Any, model: str, messages: list[dict[str, Any]], answer: str,
             tools: Optional[list[Any]], store: Any, session_id: str, episode_id: int) -> bool:
    """Starts naming in the background when the episode is due a name; True if it started."""
    upto = due(store, session_id, episode_id)
    if upto is None or not (answer or "").strip():
        return False
    task = asyncio.create_task(name_topic(provider, model, list(messages), answer, tools, store, session_id,
                                          episode_id, upto))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return True
