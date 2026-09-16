"""
Pending confirmations and what happens when nobody answers them.

A CONFIRMATION_REQUIRED tool call returns an ``act_<hash>`` token and the turn ends; the
action only runs if a later turn carries that token in ``approved_action_ids``. On its own
that leaves two gaps for a hands-free session: an unanswered ask just goes quiet, and the
token never expires, so a "yes" that arrives minutes later still runs whatever was asked.

The registry here closes both. Every ask is recorded with a deadline; the watcher announces
the ones that lapse through the awareness monitor (so they are spoken and shown like any
other thing Jarvis says unprompted), and a lapsed token is refused when it finally shows up
-- the user hears that it timed out and is asked again rather than having a stale approval
silently honoured. That is the concrete meaning of ``CONFIRMATION_TIMEOUT_ACTION = "deny"``.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from app.awareness.observations import Observation, Severity

logger = logging.getLogger("jarvis.agent.confirmations")

CONFIRMATION_TIMEOUT_KIND = "confirmation_timeout"


@dataclass
class PendingConfirmation:
    action_id: str
    session_id: str
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    description: str = ""
    asked_at: float = field(default_factory=time.time)
    expires_at: float = 0.0

    def to_dict(self, now: Optional[float] = None) -> dict[str, Any]:
        current = now if now is not None else time.time()
        return {
            "action_id": self.action_id,
            "session_id": self.session_id,
            "tool": self.tool,
            "args": dict(self.args),
            "description": self.description,
            "asked_at": self.asked_at,
            "expires_at": self.expires_at,
            "seconds_remaining": max(0.0, round(self.expires_at - current, 1)),
        }


class ConfirmationRegistry:
    """Everything Jarvis has asked permission for and not yet heard back about."""

    def __init__(self, timeout_seconds: float = 90.0, remembered_expiries: int = 256):
        # 0 (or less) means asks never lapse -- the pre-registry behaviour.
        self.timeout_seconds = timeout_seconds
        self._pending: "OrderedDict[str, PendingConfirmation]" = OrderedDict()
        # Tokens that lapsed, kept so a late approval can be told apart from a token
        # this process never issued (a backend restart), which still passes through.
        self._expired: deque[str] = deque(maxlen=remembered_expiries)
        # Set by ask(): True when any action just asked about had previously lapsed.
        self.last_ask_was_repeat = False

    # ------------------------------------------------------------------ asks

    def ask(
        self,
        session_id: str,
        pending: list[dict[str, Any]],
        now: Optional[float] = None,
    ) -> list[PendingConfirmation]:
        """Record the actions a turn just asked about. Re-asking the same action resets its clock."""
        from app.agent.permissions import _describe_pending_action

        current = now if now is not None else time.time()
        deadline = current + self.timeout_seconds if self.timeout_seconds > 0 else float("inf")
        recorded = []
        self.last_ask_was_repeat = False
        for item in pending:
            action_id = str(item.get("action_id") or "")
            if not action_id:
                continue
            entry = PendingConfirmation(
                action_id=action_id,
                session_id=session_id or "default",
                tool=str(item.get("tool") or ""),
                args=dict(item.get("args") or {}),
                description=_describe_pending_action(item),
                asked_at=current,
                expires_at=deadline,
            )
            if action_id in self._expired:
                # The same action coming back after its token lapsed: the user approved
                # too late (or asked again), so the prompt should say it is a re-ask.
                self._expired.remove(action_id)
                self.last_ask_was_repeat = True
            self._pending.pop(action_id, None)
            self._pending[action_id] = entry
            recorded.append(entry)
        return recorded

    def pending(self, session_id: Optional[str] = None) -> list[PendingConfirmation]:
        return [
            p for p in self._pending.values()
            if session_id is None or p.session_id == session_id
        ]

    def get(self, action_id: str) -> Optional[PendingConfirmation]:
        return self._pending.get(action_id)

    # --------------------------------------------------------------- answers

    def resolve(self, action_ids: list[str]) -> list[PendingConfirmation]:
        """Forget these asks (approved or denied); returns the ones that were actually pending."""
        resolved = []
        for action_id in action_ids:
            entry = self._pending.pop(action_id, None)
            if entry is not None:
                resolved.append(entry)
        return resolved

    def deny(self, action_id: str) -> Optional[PendingConfirmation]:
        """The user said no: drop the ask quietly, so no timeout is announced for it."""
        entry = self._pending.pop(action_id, None)
        if entry is not None:
            logger.info("Confirmation %s (%s) denied by user", action_id, entry.description)
        return entry

    def filter_approvals(
        self, action_ids: Optional[list[str]], now: Optional[float] = None
    ) -> tuple[list[str], list[str]]:
        """
        Split a turn's ``approved_action_ids`` into the ones that may run and the ones that
        lapsed. Approving consumes the ask. Tokens this registry never saw pass through
        untouched: the permission gate still hashes them, so they cannot approve anything
        other than the exact action they were minted for.
        """
        if not action_ids:
            return [], []
        allowed: list[str] = []
        stale: list[str] = []
        current = now if now is not None else time.time()
        for action_id in action_ids:
            entry = self._pending.get(action_id)
            if entry is not None and entry.expires_at <= current:
                # Lapsed but not yet swept by the watcher -- same answer either way.
                self._pending.pop(action_id, None)
                self._expired.append(action_id)
                stale.append(action_id)
            elif entry is None and action_id in self._expired:
                stale.append(action_id)
            else:
                self._pending.pop(action_id, None)
                allowed.append(action_id)
        if stale:
            logger.warning("Refusing %d lapsed confirmation token(s): %s", len(stale), stale)
        return allowed, stale

    # --------------------------------------------------------------- expiry

    def expire(self, now: Optional[float] = None) -> list[PendingConfirmation]:
        """Remove and return every ask whose deadline has passed."""
        current = now if now is not None else time.time()
        lapsed = [p for p in self._pending.values() if p.expires_at <= current]
        for entry in lapsed:
            self._pending.pop(entry.action_id, None)
            self._expired.append(entry.action_id)
        return lapsed

    def clear(self) -> None:
        self._pending.clear()
        self._expired.clear()


def build_timeout_observation(lapsed: list[PendingConfirmation], persona: Any) -> Observation:
    """
    What Jarvis says when an ask goes unanswered. One observation per session, so several
    actions asked in one turn lapse as one sentence rather than a barrage.
    """
    from app.awareness.briefing import address_suffix

    address = address_suffix(persona)
    descriptions = [p.description or p.tool or "that action" for p in lapsed]
    if len(descriptions) == 1:
        what = descriptions[0]
        spoken = (
            f"I did not hear back about whether to {what}, so I have not done it{address}. "
            "Ask again if you still want it."
        )
        title = f"Confirmation timed out: {what}"
    else:
        what = "; ".join(descriptions)
        spoken = (
            f"I did not hear back about {len(descriptions)} actions, so I have not done "
            f"any of them{address}. Ask again if you still want them."
        )
        title = f"{len(descriptions)} confirmations timed out"
    detail = f"No answer within the confirmation window — not run: {what}."
    return Observation(
        kind=CONFIRMATION_TIMEOUT_KIND,
        severity=Severity.WARNING,
        title=title,
        detail=detail,
        spoken=spoken,
        data={
            "session_id": lapsed[0].session_id,
            "action_ids": [p.action_id for p in lapsed],
            "tools": [p.tool for p in lapsed],
        },
    )


class ConfirmationWatcher:
    """Sweeps the registry and announces lapsed asks through the awareness monitor."""

    def __init__(
        self,
        registry: ConfirmationRegistry,
        monitor: Any,
        persona_provider: Callable[[], Any],
        check_seconds: float = 5.0,
    ):
        self.registry = registry
        self.monitor = monitor
        self.persona_provider = persona_provider
        self.check_seconds = check_seconds
        self._task: Optional[asyncio.Task] = None
        self._running = False

    def sweep_once(self, now: Optional[float] = None) -> list[Observation]:
        lapsed = self.registry.expire(now)
        if not lapsed:
            return []
        by_session: dict[str, list[PendingConfirmation]] = {}
        for entry in lapsed:
            by_session.setdefault(entry.session_id, []).append(entry)
        emitted = []
        persona = self.persona_provider()
        for entries in by_session.values():
            observation = build_timeout_observation(entries, persona)
            logger.info("Confirmation timed out for %s", [e.description for e in entries])
            emitted.append(self.monitor.emit(observation))
        return emitted

    async def _loop(self) -> None:
        logger.info("Confirmation watcher started (check=%.0fs)", self.check_seconds)
        while self._running:
            try:
                self.sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Confirmation sweep failed: %s", e)
            await asyncio.sleep(self.check_seconds)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        logger.info("Confirmation watcher stopped")


_registry: Optional[ConfirmationRegistry] = None


def get_confirmation_registry() -> ConfirmationRegistry:
    global _registry
    if _registry is None:
        from app.config import settings

        _registry = ConfirmationRegistry(timeout_seconds=settings.confirmation_timeout_seconds)
    return _registry
