"""
Turns paused for approval, kept so an approval resumes them instead of re-running them.

An approval arrives as a new request: the client re-sends the message with the approved
``act_`` ids. Re-running the whole turn worked while the calls' arguments came out the same
every time (open notepad, a path), but a call whose arguments the model writes -- ``write_file``
with a summary in ``content`` -- comes out different on the re-run, hashes to a new id and asks
again, forever (device test 2026-10-17). So the stream loop parks the turn where it stopped: its
messages so far, the round's tool calls, what it has done and said. The continuation runs exactly
those calls and carries on from the next round.

One parked turn per session, taken once, dropped when denied, by the session's purge or after
``PARK_SECONDS``. A continuation with nothing parked (a restart, an expiry) re-runs the turn as before.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from app.agent.taint import PARK_SECONDS


@dataclass
class ParkedTurn:
    pending_ids: set[str]  # the approval card's action ids
    messages: list[dict[str, Any]]  # system prompt + conversation, up to the paused round
    content: str  # what the model wrote beside its calls in the paused round
    tool_calls: list[dict[str, Any]]  # the paused round's calls, as the model made them
    round_index: int
    tools: list[Any]
    model: str
    thinking: Optional[bool]
    profile: str
    route_reason: str
    chat_mode: Any
    active_skills: list[str]
    workspace_path: Optional[str]
    tool_context: dict[str, Any]
    max_iterations: int
    tools_used: list[dict[str, Any]]
    metrics: Any
    call_history: Any
    nudged: bool
    claim_checked: bool
    tainted_by: Optional[str]
    expires_at: float = field(default_factory=lambda: time.monotonic() + PARK_SECONDS)


_lock = threading.Lock()
_parked: dict[str, ParkedTurn] = {}


def park(session_id: str, turn: ParkedTurn) -> None:
    with _lock:
        _parked[session_id] = turn


def take(session_id: str, approved_ids: Optional[list[str]]) -> Optional[ParkedTurn]:
    """The session's parked turn when this approval answers its card (any of its ids), consumed
    once; None otherwise. A stale park is dropped on the way."""
    if not approved_ids:
        return None
    with _lock:
        turn = _parked.get(session_id)
        if turn is None:
            return None
        if turn.expires_at <= time.monotonic():
            _parked.pop(session_id, None)
            return None
        if not turn.pending_ids.intersection(approved_ids):
            return None
        return _parked.pop(session_id)


def drop_action(action_id: str) -> None:
    """A denied card: the turn it paused will not go on."""
    with _lock:
        for session_id, turn in list(_parked.items()):
            if action_id in turn.pending_ids:
                _parked.pop(session_id, None)


def forget(session_id: str) -> None:
    with _lock:
        _parked.pop(session_id, None)
