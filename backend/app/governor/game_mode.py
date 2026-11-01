"""Game mode (PLAN 4.16b): step aside *before* a game starts.

The governor only reacts once another process has grown on the card, and on 2026-10-19 God of
War III reserved 8 GB at start, was refused by Windows (RAM at 95.5%) and exited in under a
second -- before any reactive rule could unload anything. Game mode is the pre-launch switch:
turning it on unloads the chat model, stops every sidecar and pauses the governor, and while it
is on nothing may load a model again (``RuntimeProcessManager.ensure_running`` and the sidecars
ask :func:`blocked` first). A chat or voice message is answered in code meanwhile, since there
is no model to answer it; "game mode off" there turns it off. Not persisted: a restarted
backend starts with it off.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Awaitable, Callable, Iterable, Iterator, Optional

logger = logging.getLogger(__name__)

_GAME = r"(?:game|gaming)\s*mode"
_ON = re.compile(
    rf"^(?:(?:turn|switch|put)\s+on\s+(?:the\s+)?{_GAME}"
    rf"|(?:turn|switch|put)\s+(?:the\s+)?{_GAME}\s+on"
    rf"|(?:start|enable|enter|activate|go\s+into)\s+(?:the\s+)?{_GAME}"
    rf"|{_GAME}\s+on)$"
)
_OFF = re.compile(
    rf"^(?:(?:turn|switch)\s+off\s+(?:the\s+)?{_GAME}"
    rf"|(?:turn|switch)\s+(?:the\s+)?{_GAME}\s+off"
    rf"|(?:stop|disable|exit|leave|end|quit)\s+(?:the\s+)?{_GAME}"
    rf"|{_GAME}\s+off"
    r"|i'?m\s+done\s+(?:gaming|playing)|done\s+(?:gaming|playing))$"
)
_LEAD = re.compile(r"^(?:(?:hey|ok|okay)\s+)?(?:jarvis\b[,\s]*)?(?:please\s+)?")
_TAIL = re.compile(r"(?:[,\s]+(?:please|now|jarvis|sir|thanks|thank\s+you))*[\s.!]*$")

BLOCKED = "Game mode is on, so no model loads until it is turned off."
_IDLE_REPLY = ("Game mode is on, so no model is loaded and I can't answer that right now. Say "
               "\"game mode off\" -- or use the tray icon or the status popover -- to bring me back.")


def command(message: str) -> Optional[str]:
    """"on" / "off" when the message is a Game mode switch and nothing else, else None. A question
    ("what is game mode?") or a longer sentence is not a switch."""
    text = (message or "").strip().lower().replace("’", "'")
    if not text or len(text) > 60 or "?" in text:
        return None
    text = _TAIL.sub("", _LEAD.sub("", text)).strip()
    if _ON.match(text):
        return "on"
    if _OFF.match(text):
        return "off"
    return None


class GameMode:
    def __init__(self) -> None:
        self.active = False
        self.since: Optional[float] = None
        self.stopped: list[str] = []
        self.freed_vram_mb: Optional[float] = None
        # Wired by app.main (kept as callables so tests need no GPU, model or sidecar).
        self._pause: Callable[[], None] = lambda: None
        self._resume: Callable[[], None] = lambda: None
        self._release: Callable[[], Awaitable[list[str]]] = _nothing_to_release
        self._vram_used_mb: Callable[[], Optional[float]] = lambda: None
        self._after_turn: list[tuple[str, Optional[Callable[[], str]]]] = []
        self._games: list[tuple[str, Callable[[str], bool]]] = []

    def configure(self, *, pause: Callable[[], None], resume: Callable[[], None],
                  release: Callable[[], Awaitable[list[str]]],
                  vram_used_mb: Callable[[], Optional[float]]) -> None:
        self._pause, self._resume, self._release, self._vram_used_mb = pause, resume, release, vram_used_mb

    def status(self) -> dict[str, Any]:
        return {"active": self.active, "since": self.since, "stopped": list(self.stopped),
                "freed_vram_mb": self.freed_vram_mb}

    async def enter(self) -> dict[str, Any]:
        if self.active:
            return self.status()
        # The gate closes first, so nothing reloads in the gap while the model is being stopped.
        self.active, self.since = True, time.time()
        self._pause()
        before = _safe(self._vram_used_mb)
        try:
            self.stopped = await self._release()
        except Exception:
            logger.exception("Game mode: releasing the model and sidecars failed part-way")
            self.stopped = []
        after = _safe(self._vram_used_mb)
        self.freed_vram_mb = max(0.0, before - after) if before is not None and after is not None else None
        logger.info("Game mode ON: stopped %s; %s MiB VRAM freed.",
                    ", ".join(self.stopped) or "nothing (nothing was running)",
                    "?" if self.freed_vram_mb is None else f"{self.freed_vram_mb:.0f}")
        return self.status()

    async def leave(self) -> dict[str, Any]:
        if not self.active:
            return self.status()
        self.active, self.since, self.stopped, self.freed_vram_mb = False, None, [], None
        self._resume()
        logger.info("Game mode OFF: models load again on demand.")
        return self.status()

    # --- the model asking for Game mode (PLAN 4.16b) ----------------------------------------------
    # A tool runs while the model is mid-reply, and Game mode unloads that model, so a request is
    # parked and honoured when the turn is over (``run_after_turn``, called by the chat endpoints,
    # which are also what set ``turn_in_progress``). A launch parked here starts only after the
    # GPU is free: God of War III needed that, and failed within a second when it was not.

    def request_after_turn(self, reason: str, launch: Optional[Callable[[], str]] = None) -> None:
        self._after_turn.append((reason, launch))

    def drop_after_turn(self) -> None:
        self._after_turn.clear()

    async def run_after_turn(self) -> None:
        requests, self._after_turn = self._after_turn, []
        if not requests:
            return
        logger.info("Game mode requested by the model: %s", "; ".join(r for r, _ in requests))
        await self.enter()
        for reason, launch in requests:
            if launch is None:
                continue
            try:
                logger.info("Game mode: %s", await asyncio.to_thread(launch))
            except Exception:
                logger.exception("Game mode: the launch parked for '%s' failed", reason)

    def set_games(self, games: Iterable[tuple[str, Callable[[str], bool]]]) -> None:
        """(label, matches(process or app name)) for each watchlisted game."""
        self._games = list(games)

    def game_for(self, name_or_path: str, target: str) -> Optional[str]:
        """The watchlisted game's label when a launch of *name_or_path* (resolved to *target*)
        would start one, else None."""
        wanted = {_stem(name_or_path), _stem(target)}
        for label, matches in self._games:
            if _stem(label) in wanted or matches(os.path.basename(target)) or matches(os.path.basename(name_or_path)):
                return label
        return None

    async def reply_for(self, message: str) -> Optional[str]:
        """The answer in code for a chat/voice message while Game mode matters, else None (the
        turn goes to the model as usual)."""
        cmd = command(message)
        if cmd == "on":
            if self.active:
                return "Game mode is already on."
            await self.enter()
            freed = f" ({self.freed_vram_mb / 1024:.1f} GB of VRAM back)" if self.freed_vram_mb and self.freed_vram_mb >= 100 else ""
            return (f"Game mode on: the model is unloaded{freed} and nothing loads until you turn it off. "
                    "Say \"game mode off\" when you're done.")
        if cmd == "off":
            if not self.active:
                return "Game mode is already off."
            await self.leave()
            return "Game mode off. The model loads again with your next message."
        if self.active:
            return _IDLE_REPLY
        return None


def _stem(name: str) -> str:
    base = os.path.basename((name or "").strip().strip('"')).lower()
    return base[:-4] if base.endswith((".exe", ".lnk")) else base


# Set by the chat endpoints around a turn: only a launch inside one can wait for the turn's end.
turn_in_progress: ContextVar[bool] = ContextVar("game_mode_turn_in_progress", default=False)


async def _nothing_to_release() -> list[str]:
    return []


def _safe(fn: Callable[[], Optional[float]]) -> Optional[float]:
    try:
        return fn()
    except Exception:
        return None


game_mode = GameMode()


# Short holds besides Game mode: a tool update swapping llama.cpp or a venv's packages under the
# processes that use them (app/updates). Same gate, so nothing restarts mid-swap.
_holds: dict[str, str] = {}


@contextmanager
def hold(key: str, reason: str) -> Iterator[None]:
    _holds[key] = reason
    try:
        yield
    finally:
        _holds.pop(key, None)


def blocked() -> Optional[str]:
    """Why a model or sidecar may not start now, or None."""
    if game_mode.active:
        return BLOCKED
    return next(iter(_holds.values()), None)
