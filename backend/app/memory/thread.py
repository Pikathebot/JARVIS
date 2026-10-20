"""The one continuing Freeform thread (PLAN 4.18).

Freeform is one thread on screen, but the model only ever sees the open **episode** of it. A new
episode opens on the first message after a 3-hour gap, or when the user cuts early ("new topic",
the client's New topic button). Within an episode the history is append-only -- no sliding
window -- so llama-server keeps reusing its KV cache turn after turn; when an episode outgrows its
budget, its oldest half is folded away *once* (one re-prefill) and appending resumes.

What the model gets beyond the episode's own messages is the **preamble**, attached after the
user's words on the episode's first visible message: the digest of earlier episodes (fixed when the
episode opens) and, after a fold, a note standing in for the folded messages. Both stay
byte-identical for the rest of the episode, so the prefix contract (CLAUDE.md) holds.

Phase 1 is the plumbing. Episode records (title, summary, decisions, open items) and real fold
summaries are written by the main model in the background in phase 2; until a record exists the
digest names an episode by its time span, and a fold note just says what was folded.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from app.config import settings
from app.memory.context_manager import _replay_units
from app.memory.token_counter import TokenCounter

THREAD_SESSION_ID = "jarvis-thread"
DIGEST_EPISODES = 5

# The whole message ("new topic", "Jarvis, new topic.") or a lead-in ("new topic: what's ...").
_NEW_TOPIC = re.compile(
    r"^\s*(?:(?:hey|ok|okay)\s+)?(?:jarvis[\s,]+)?(?:let'?s\s+)?(?:start\s+a\s+)?new\s+topic\b[\s.!:,;-]*",
    re.IGNORECASE,
)


def is_thread(session_id: Optional[str]) -> bool:
    return session_id == THREAD_SESSION_ID


def split_new_topic(message: str) -> tuple[bool, str]:
    """(asked for a new topic, the rest of the message)."""
    match = _NEW_TOPIC.match(message or "")
    if not match:
        return False, message
    return True, message[match.end():].strip()


@dataclass
class EpisodeContext:
    episode: dict[str, Any]
    opened: bool              # this turn opened the episode
    rows: list[dict[str, Any]]  # stored rows the model sees (after the fold point)
    preamble: str             # digest + fold note, attached to the first visible user message
    folded_now: bool = False


def _clip(text: str, limit: int) -> str:
    """The digest's share of a record field: the record keeps it whole (for recall), the digest
    rides on every turn of an episode and stays ~300-400 tokens."""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3].rsplit(" ", 1)[0] + "..."


def _clock(ts: Optional[float]) -> str:
    return datetime.fromtimestamp(ts).strftime("%a %d %b %H:%M") if ts else "?"


def todays_briefing(store: Any, session_id: str, now: float, exclude_id: Optional[int] = None) -> str:
    """The day's briefing text when this is the first episode since the day began (4 AM), else ''.
    The briefing itself is composed and emitted by ``app.awareness.day_briefing``; the first
    episode of the day carries it so the model knows what the user was told this morning."""
    from app.awareness import day_briefing

    at = datetime.fromtimestamp(now)
    day = day_briefing.briefing_day(at)
    day_start = (datetime.combine(day, datetime.min.time()) + day_briefing.DAY_STARTS_AT).timestamp()
    if any(e["started_at"] >= day_start and e["id"] != exclude_id for e in store.list_episodes(session_id, limit=3)):
        return ""
    return day_briefing.briefing_text(day)


def build_digest(store: Any, session_id: str, now: float, exclude_id: Optional[int] = None) -> str:
    """Earlier episodes, newest first, as the new episode's opening context. Uses each episode's
    record where one exists; otherwise its time span. The first episode of a day also carries the
    morning briefing. Empty when there is neither."""
    briefing = todays_briefing(store, session_id, now, exclude_id)
    earlier = [e for e in store.list_episodes(session_id, limit=DIGEST_EPISODES + 1)
               if e["record_status"] != "open" and e["id"] != exclude_id][:DIGEST_EPISODES]
    if not earlier and not briefing:
        return ""
    lines = ["[Earlier in this conversation -- context, not part of the user's message; "
             "ask the user for anything not here:"]
    if briefing:
        lines.append("This morning's briefing: " + " ".join(briefing.split()))
    for ep in earlier:
        span = f"{_clock(ep['started_at'])}-{datetime.fromtimestamp(ep['ended_at']).strftime('%H:%M') if ep.get('ended_at') else '?'}"
        lines.append(f"- {span}: {ep.get('title') or 'untitled (summary pending)'}")
    last = earlier[0] if earlier else {}
    if last.get("summary"):
        lines.append(f"Last time: {_clip(last['summary'], 500)}")
    if last.get("decisions"):
        lines.append(f"Decided: {_clip(last['decisions'], 300)}")
    open_items = [_clip(ep["open_items"], 200) for ep in earlier if ep.get("open_items")]
    if open_items:
        lines.append("Open items: " + " | ".join(open_items))
    return "\n".join(lines) + "]"


class ThreadEpisodes:
    def __init__(self, store: Any, token_counter: Optional[TokenCounter] = None,
                 gap_seconds: Optional[float] = None, budget_tokens: Optional[int] = None) -> None:
        self.store = store
        self.counter = token_counter or TokenCounter()
        self.gap_seconds = gap_seconds if gap_seconds is not None else settings.thread_episode_gap_hours * 3600
        self.budget_tokens = budget_tokens or settings.thread_episode_budget_tokens

    # ---- boundary ------------------------------------------------------------------------

    def current(self, session_id: str, new_topic: bool = False, now: Optional[float] = None) -> tuple[dict[str, Any], bool]:
        """The episode this turn belongs to, opening (and closing the previous) when due."""
        now = now or time.time()
        episode = self.store.open_episode(session_id)
        last = self.store.last_message_meta(session_id)
        if episode is not None and not new_topic:
            last_activity = max(episode["started_at"], (last or {}).get("created_at") or 0.0)
            if now - last_activity < self.gap_seconds:
                return episode, False
        return self.open_new(session_id, now, closing=episode, last=last), True

    def open_new(self, session_id: str, now: float, closing: Optional[dict[str, Any]] = None,
                 last: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        if closing is not None:
            ended = (last or {}).get("created_at") or closing["started_at"]
            # pending = the record (phase 2's background summary) is still to be written
            self.store.update_episode(closing["id"], ended_at=max(ended, closing["started_at"]),
                                      record_status="pending")
        digest = build_digest(self.store, session_id, now)
        return self.store.create_episode(session_id, started_at=now,
                                         start_after_message_id=(last or {}).get("id") or 0, digest=digest)

    # ---- what the model sees -------------------------------------------------------------

    def context(self, session_id: str, new_topic: bool = False, now: Optional[float] = None) -> EpisodeContext:
        episode, opened = self.current(session_id, new_topic=new_topic, now=now)
        rows = self.visible_rows(session_id, episode)
        folded_now = False
        if self.needs_fold(rows):
            episode, kept = self.fold(episode, rows)
            folded_now, rows = kept is not rows, kept
        return EpisodeContext(episode=episode, opened=opened, rows=rows,
                              preamble=self.preamble(episode), folded_now=folded_now)

    @staticmethod
    def preamble(episode: dict[str, Any]) -> str:
        return "\n\n".join(p for p in (episode.get("digest"), episode.get("fold_note")) if p)

    def _tokens(self, rows: list[dict[str, Any]]) -> int:
        clean = [{k: v for k, v in r.items() if k not in ("id", "created_at")} for r in rows]
        return sum(self.counter.count_messages(unit) for unit in _replay_units(clean))

    def fold_point(self, rows: list[dict[str, Any]]) -> int:
        """Index of the first row to keep: the newest rows worth about half the budget, starting
        at a user message (so a tool call and its result are never split). 0 or len(rows) =
        nothing sensible to cut (one enormous turn)."""
        keep_from = len(rows)
        kept = 0
        for i in range(len(rows) - 1, -1, -1):
            kept += self._tokens([rows[i]])
            if rows[i].get("role") == "user":
                keep_from = i
                if kept >= self.budget_tokens // 2:
                    break
        return keep_from

    def needs_fold(self, rows: list[dict[str, Any]], ratio: float = 1.0) -> bool:
        return self._tokens(rows) > self.budget_tokens * ratio

    def fold(self, episode: dict[str, Any], rows: list[dict[str, Any]],
             note: Optional[str] = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Fold everything before ``fold_point``. ``note`` is the summary of the folded rows (the
        keeper's, written between turns); without one -- a fold forced mid-turn -- the note only
        says what was folded."""
        keep_from = self.fold_point(rows)
        if keep_from == 0 or keep_from >= len(rows):
            return episode, rows
        folded, kept_rows = rows[:keep_from], rows[keep_from:]
        if not note:
            prior = episode.get("fold_note")
            count = len([r for r in folded if r.get("role") in ("user", "assistant")])
            note = (f"[Earlier in this episode, folded away: {count} messages from "
                    f"{_clock(folded[0].get('created_at'))} to {_clock(folded[-1].get('created_at'))}"
                    f"{' (after an earlier fold)' if prior else ''}. Ask the user if something from them matters.]")
        fields = {"fold_upto_message_id": folded[-1]["id"], "fold_note": note}
        self.store.update_episode(episode["id"], **fields)
        return {**episode, **fields}, kept_rows

    def visible_rows(self, session_id: str, episode: dict[str, Any]) -> list[dict[str, Any]]:
        after = max(episode["start_after_message_id"], episode.get("fold_upto_message_id") or 0)
        return self.store.get_message_rows(session_id, after_id=after)
