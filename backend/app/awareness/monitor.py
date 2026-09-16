"""
The awareness monitor.

Polls the machine, runs the rules, and decides what is worth saying. The point
of the monitor is restraint: a naive loop that fires every time VRAM is above
88% would say the same sentence every two seconds. So it tracks which
conditions are already active, escalates only when severity rises, re-states a
standing condition at most once per cooldown, and announces recovery once when
a condition clears.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any, Awaitable, Callable, Iterable, Optional

from app.awareness.observations import Observation, Severity, SystemSnapshot
from app.awareness.rules import DEFAULT_RULES, RECOVERY_TEXT, RuleFn, Thresholds

logger = logging.getLogger("jarvis.awareness")

# A proactive action: given the observation that tripped it, does something
# about it and returns a sentence describing what it did (or None to stay
# silent — e.g. because there was nothing left to do).
ActionFn = Callable[[Observation], Awaitable[Optional[str]]]


class AwarenessMonitor:
    """Watches system state and emits observations worth interrupting for."""

    def __init__(
        self,
        governor: Optional[Any] = None,
        rules: Optional[Iterable[RuleFn]] = None,
        thresholds: Optional[Thresholds] = None,
        poll_seconds: float = 20.0,
        restate_cooldown_seconds: float = 300.0,
        history_size: int = 100,
        min_speak_severity: Severity = Severity.WARNING,
        actions: Optional[dict[str, ActionFn]] = None,
        action_min_severity: Severity = Severity.CRITICAL,
        process_watchlist: Optional[Iterable[Any]] = None,
    ):
        self.governor = governor
        # The process watcher's rules, so a heavy app's own processes can be measured.
        self.process_watchlist = list(process_watchlist) if process_watchlist else []
        # label -> first time this monitor saw it, for "has been running for N minutes".
        self._heavy_since: dict[str, float] = {}
        self.rules = tuple(rules) if rules is not None else DEFAULT_RULES
        self.thresholds = thresholds or Thresholds()
        self.poll_seconds = poll_seconds
        self.restate_cooldown_seconds = restate_cooldown_seconds
        self.min_speak_severity = min_speak_severity
        # kind -> action. Only ever invoked once per escalation into
        # action_min_severity, not on every poll while the condition holds.
        self.actions: dict[str, ActionFn] = dict(actions) if actions else {}
        self.action_min_severity = action_min_severity
        self.actions_enabled = True

        self.enabled = True
        self._history: deque[Observation] = deque(maxlen=history_size)
        self._seq = 0
        # kind -> (severity, last_emitted_at)
        self._active: dict[str, tuple[Severity, float]] = {}
        # Kinds an action has already been fired for during their current
        # active streak, so a still-critical condition does not re-trigger
        # the action every poll.
        self._actioned: set[str] = set()
        self._last_snapshot: Optional[SystemSnapshot] = None
        self._subscribers: list[asyncio.Queue] = []
        self._task: Optional[asyncio.Task] = None
        self._running = False

    # ------------------------------------------------------------ sampling

    def collect_snapshot(self) -> SystemSnapshot:
        """Sample hardware state. Missing sources degrade to defaults."""
        snapshot = SystemSnapshot()

        metrics = None
        if self.governor is not None:
            try:
                metrics = self.governor.collect_metrics()
            except Exception as e:
                logger.debug("Governor metrics unavailable: %s", e)

        if metrics is not None:
            snapshot.cpu_percent = getattr(metrics, "cpu_percent", 0.0)
            snapshot.ram_percent = getattr(metrics, "ram_percent", 0.0)
            snapshot.ram_used_mb = getattr(metrics, "ram_used_mb", 0.0)
            snapshot.ram_total_mb = getattr(metrics, "ram_total_mb", 0.0)
            snapshot.gpu_available = getattr(metrics, "gpu_available", False)
            snapshot.gpu_name = getattr(metrics, "gpu_name", None)
            snapshot.gpu_util_percent = getattr(metrics, "gpu_util_percent", 0.0)
            snapshot.vram_used_mb = getattr(metrics, "vram_used_mb", 0.0)
            snapshot.vram_total_mb = getattr(metrics, "vram_total_mb", 0.0)
            snapshot.vram_free_mb = getattr(metrics, "vram_free_mb", 0.0)
            snapshot.vram_util_percent = getattr(metrics, "vram_util_percent", 0.0)
            snapshot.gpu_temp_c = getattr(metrics, "gpu_temp_c", None)
            snapshot.throttled = getattr(metrics, "throttled", False)
            snapshot.throttle_reasons = list(getattr(metrics, "throttle_reasons", []) or [])

        if self.governor is not None:
            try:
                snapshot.governor_status = self.governor.status.value
                snapshot.model_unloaded = bool(self.governor.model_unloaded)
            except Exception as e:
                logger.debug("Governor status unavailable: %s", e)

        try:
            import psutil

            disk = psutil.disk_usage(".")
            snapshot.disk_free_gb = disk.free / (1024**3)
            snapshot.disk_total_gb = disk.total / (1024**3)
            snapshot.disk_percent = disk.percent

            battery = psutil.sensors_battery()
            if battery is not None:
                snapshot.battery_percent = float(battery.percent)
                snapshot.battery_plugged = bool(battery.power_plugged)
        except Exception as e:
            logger.debug("psutil sampling failed: %s", e)

        # Heavy external apps are surfaced by the governor as throttle reasons.
        snapshot.heavy_apps = [
            reason.split(":", 1)[1].strip()
            for reason in snapshot.throttle_reasons
            if reason.lower().startswith("external app")
        ]
        snapshot.heavy_app_details = self._heavy_app_details(
            snapshot.heavy_apps, snapshot.timestamp, vram_total_mb=snapshot.vram_total_mb
        )

        self._last_snapshot = snapshot
        return snapshot

    def _heavy_app_details(
        self, labels: list[str], now: float, vram_total_mb: Optional[float] = None
    ) -> dict[str, dict[str, float]]:
        """How long each heavy app has been around, plus what it holds when that can be read."""
        for label in list(self._heavy_since):
            if label not in labels:
                del self._heavy_since[label]
        for label in labels:
            self._heavy_since.setdefault(label, now)
        if not labels:
            return {}

        stats: dict[str, dict[str, float]] = {}
        if self.process_watchlist:
            try:
                from app.awareness.actions import heavy_app_stats

                stats = heavy_app_stats(labels, self.process_watchlist, vram_total_mb=vram_total_mb)
            except Exception as e:
                logger.debug("Heavy app stats failed: %s", e)
        return {
            label: {"seconds": now - self._heavy_since[label], **stats.get(label, {})}
            for label in labels
        }

    # ---------------------------------------------------------- evaluation

    def evaluate(self, snapshot: SystemSnapshot) -> list[Observation]:
        """
        Run the rules over one snapshot and return only what should be said now.
        """
        now = snapshot.timestamp or time.time()
        tripped: dict[str, Observation] = {}

        for rule in self.rules:
            try:
                observation = rule(snapshot, self.thresholds)
            except Exception as e:
                logger.warning("Awareness rule %s failed: %s", getattr(rule, "__name__", rule), e)
                continue
            if observation is not None:
                tripped[observation.kind] = observation

        emitted: list[Observation] = []

        for kind, observation in tripped.items():
            previous = self._active.get(kind)
            if previous is None:
                should_emit = True
            else:
                previous_severity, last_at = previous
                escalated = observation.severity.rank > previous_severity.rank
                stale = (now - last_at) >= self.restate_cooldown_seconds
                should_emit = escalated or stale

            if should_emit:
                self._active[kind] = (observation.severity, now)
                emitted.append(observation)
            else:
                # Keep the recorded severity current without re-announcing it.
                self._active[kind] = (observation.severity, self._active[kind][1])

        # Anything previously active and no longer tripped has recovered.
        for kind in list(self._active):
            if kind in tripped:
                continue
            del self._active[kind]
            self._actioned.discard(kind)
            emitted.append(
                Observation(
                    kind=kind,
                    severity=Severity.INFO,
                    title=RECOVERY_TEXT.get(kind, "Condition cleared"),
                    detail=RECOVERY_TEXT.get(kind, "Condition cleared"),
                    spoken=RECOVERY_TEXT.get(kind, "That condition has cleared."),
                    resolved=True,
                )
            )

        for observation in emitted:
            self._seq += 1
            observation.seq = self._seq
            self._history.append(observation)

        return emitted

    def should_speak(self, observation: Observation) -> bool:
        """Only interrupt out loud for things that actually matter."""
        if observation.resolved:
            return False
        return observation.severity.rank >= self.min_speak_severity.rank

    # ------------------------------------------------------------ delivery

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        if queue in self._subscribers:
            self._subscribers.remove(queue)

    def _publish(self, observations: list[Observation]) -> None:
        for observation in observations:
            payload = observation.to_dict()
            payload["speak"] = self.should_speak(observation)
            for queue in list(self._subscribers):
                try:
                    queue.put_nowait(payload)
                except asyncio.QueueFull:
                    # A stalled client must not block the monitor.
                    logger.debug("Dropping observation for a saturated subscriber")

    def emit(self, observation: Observation) -> Observation:
        """
        Publish an observation from outside the rule loop (e.g. a scheduled
        routine). Goes through the same history/SSE/speak path as a rule
        trip, so callers other than the monitor can still interrupt the user.
        """
        self._seq += 1
        observation.seq = self._seq
        self._history.append(observation)
        self._publish([observation])
        return observation

    def _actionable(self, observations: list[Observation]) -> list[Observation]:
        if not self.actions_enabled:
            return []
        due = []
        for observation in observations:
            if observation.resolved or observation.kind in self._actioned:
                continue
            if observation.severity.rank < self.action_min_severity.rank:
                continue
            if observation.kind not in self.actions:
                continue
            due.append(observation)
        return due

    async def _run_action(self, observation: Observation) -> None:
        action = self.actions.get(observation.kind)
        if action is None:
            return
        try:
            spoken = await action(observation)
        except Exception as e:
            logger.warning("Proactive action for %s failed: %s", observation.kind, e)
            return
        if spoken:
            self.emit(
                Observation(
                    kind=f"{observation.kind}_action",
                    severity=Severity.NOTICE,
                    title="Jarvis acted",
                    detail=spoken,
                    spoken=spoken,
                    data={"triggered_by": observation.id},
                )
            )

    async def poll_once(self) -> list[Observation]:
        snapshot = await asyncio.to_thread(self.collect_snapshot)
        observations = self.evaluate(snapshot)
        if observations:
            self._publish(observations)

        for observation in self._actionable(observations):
            self._actioned.add(observation.kind)
            asyncio.create_task(self._run_action(observation))

        return observations

    # ----------------------------------------------------------- lifecycle

    async def _loop(self) -> None:
        logger.info("Awareness monitor started (poll=%.0fs)", self.poll_seconds)
        while self._running:
            try:
                if self.enabled:
                    await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Awareness poll failed: %s", e)
            await asyncio.sleep(self.poll_seconds)

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
        logger.info("Awareness monitor stopped")

    # -------------------------------------------------------------- access

    def recent(self, limit: int = 20, since_seq: int = 0) -> list[Observation]:
        """History newest-last.  is the last seq the caller has seen."""
        items = [o for o in self._history if o.seq > since_seq]
        return items[-limit:]

    def active_observations(self) -> list[str]:
        return sorted(self._active)

    def acknowledge(self, observation_id: str) -> bool:
        for observation in self._history:
            if observation.id == observation_id:
                observation.acknowledged = True
                return True
        return False

    @property
    def last_snapshot(self) -> Optional[SystemSnapshot]:
        return self._last_snapshot

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "running": self._running,
            "poll_seconds": self.poll_seconds,
            "restate_cooldown_seconds": self.restate_cooldown_seconds,
            "min_speak_severity": self.min_speak_severity.value,
            "actions_enabled": self.actions_enabled,
            "actionable_kinds": sorted(self.actions),
            "active_conditions": self.active_observations(),
            "subscribers": len(self._subscribers),
            "latest_seq": self._seq,
            "thresholds": self.thresholds.__dict__,
        }
