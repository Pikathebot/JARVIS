"""The Freeform thread, live on every device (PLAN 4.19): a turn sent from the phone streams on the
desktop as it happens, and the other way round.

``/chat/stream`` publishes each thread turn here -- ``turn_start`` (the message, who sent it),
then every event of the turn exactly as its own client gets it, then ``turn_end`` -- and
``GET /api/thread/live`` relays them to every open client. Each frame's data carries ``_turn``
(the turn's id) and ``_origin`` (the ``X-Jarvis-Client`` id of the device that sent it), so a
client skips its own turns: it is already streaming those on its own request.

Nothing is stored here: a client that connects mid-turn sees the rest of it, and the stored
thread (``/api/thread/messages``) stays the truth -- clients reconcile with it after ``turn_end``.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger("jarvis.thread_live")

_QUEUE_SIZE = 2000  # a whole turn's tokens; a client that falls this far behind is dropped


class ThreadLive:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_SIZE)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, event: str, data: dict[str, Any], turn_id: str, origin: str) -> None:
        if not self._subscribers:
            return
        frame = (event, {**data, "_turn": turn_id, "_origin": origin})
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(frame)
            except asyncio.QueueFull:
                # A stalled client: drop it rather than hold the turn up. It reconnects and
                # catches up from the stored thread.
                logger.info("Dropping a stalled thread-live subscriber.")
                self._subscribers.discard(queue)
                try:
                    queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass


_live = ThreadLive()


def get_thread_live() -> ThreadLive:
    return _live
