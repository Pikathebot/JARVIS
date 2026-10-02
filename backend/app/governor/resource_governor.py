import asyncio
import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Optional, Union, Callable
import psutil

logger = logging.getLogger("jarvis.governor")

import warnings

# Use official nvidia-ml-py SDK
HAS_NVML = False
try:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=FutureWarning)
        import nvidia_ml_py as pynvml
    HAS_NVML = True
except ImportError:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=FutureWarning)
            import pynvml
        HAS_NVML = True
    except ImportError:
        HAS_NVML = False


class ActivityType(str, Enum):
    INFERENCING = "inferencing"
    MODEL_LOADING = "model_loading"
    RAG_INDEXING = "rag_indexing"
    SCREEN_VISION = "screen_vision"
    SCHEDULER_JOB = "scheduler_job"
    TELEGRAM_ACTION = "telegram_action"
    TTS_INFERENCE = "tts_inference"


class GovernorStatus(str, Enum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    LOADING = "LOADING"
    UNLOADED = "UNLOADED"
    ERROR = "ERROR"

    @classmethod
    def _missing_(cls, value: object) -> Any:
        if isinstance(value, str):
            v_upper = value.upper().strip()
            if v_upper == "NORMAL":
                return cls.IDLE
            if v_upper == "BUSY":
                return cls.RUNNING
            if v_upper == "THROTTLED":
                return cls.UNLOADED
        return super()._missing_(value)


# Backwards compatibility class-attribute aliases
GovernorStatus.NORMAL = GovernorStatus.IDLE  # type: ignore[attr-defined]
GovernorStatus.BUSY = GovernorStatus.RUNNING  # type: ignore[attr-defined]
GovernorStatus.THROTTLED = GovernorStatus.UNLOADED  # type: ignore[attr-defined]


@dataclass
class ActivityRecord:
    activity_id: str
    activity_type: ActivityType
    label: Optional[str]
    started_at: float = field(default_factory=time.time)


@dataclass
class UnloadReason:
    reason_id: str
    source: str  # "external_app", "metrics", "manual_override"
    label: str
    created_at: float = field(default_factory=time.time)
    expires_at: Optional[float] = None

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return time.time() >= self.expires_at


@dataclass
class SystemMetrics:
    cpu_percent: float = 0.0
    ram_percent: float = 0.0
    ram_used_mb: float = 0.0
    ram_total_mb: float = 0.0
    gpu_available: bool = False
    gpu_name: Optional[str] = None
    gpu_util_percent: float = 0.0
    vram_util_percent: float = 0.0
    vram_used_mb: float = 0.0
    vram_total_mb: float = 0.0
    vram_free_mb: float = 0.0
    model_vram_mb: float = 0.0       # what Jarvis's own loaded model accounts for
    external_vram_mb: float = 0.0    # everything else on the card (desktop, browsers, games)
    # What the external share was when the model launched (RuntimeProcessManager records it),
    # tracked downward while the model stays loaded. None when no model of ours is loaded or the
    # launch went unmeasured; the absolute floor applies then.
    external_vram_baseline_mb: Optional[float] = None
    # False when the card reports less in use than our model alone costs: WDDM has demoted part
    # of the model to shared system memory (another process asked for VRAM it could not have).
    # The external share is unknowable then -- the subtraction just clamps to zero.
    model_resident: bool = True
    gpu_temp_c: Optional[float] = None
    raw_throttled: bool = False      # Instantaneous single-poll threshold breach
    throttled: bool = False          # Debounced hysteresis state
    throttle_reasons: list[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)


@dataclass
class GovernorEvent:
    timestamp: float = field(default_factory=time.time)
    from_status: str = "IDLE"
    to_status: str = "IDLE"
    raw_reasons: list[str] = field(default_factory=list)
    metrics_snapshot: Optional[SystemMetrics] = None
    active_activities: list[str] = field(default_factory=list)


class ResourceGovernor:
    """
    Governor V2: Activity-Aware, Debounced, Overridable, Observable Resource Governor.
    Safeguards host performance, coordinates Jarvis tasks, and detects heavy external apps.
    """

    def __init__(
        self,
        enabled: bool = True,
        poll_interval: float = 1.0,
        gpu_threshold: float = 88.0,
        vram_threshold: float = 92.0,
        cpu_threshold: float = 92.0,
        ram_threshold: float = 94.0,
        sustained_breach_polls: int = 3,
        recovery_polls: int = 2,
        auto_unload_on_throttle: bool = True,
        on_throttle_unload: Optional[Any] = None,
        on_reload: Optional[Any] = None,
        startup_grace_seconds: float = 20.0,
        model_vram_mb_provider: Optional[Callable[[], float]] = None,
        external_vram_floor_mb: float = 2048.0,
        external_vram_baseline_provider: Optional[Callable[[], Optional[float]]] = None,
        external_vram_growth_mb: float = 1024.0,
        runtime_busy_provider: Optional[Callable[[], Awaitable[Optional[bool]]]] = None,
        on_paged_model_reload: Optional[Callable[[], Awaitable[bool]]] = None,
        paged_reload_reserve_mb: float = 384.0,
    ):
        self.enabled = enabled
        # A model WDDM demoted to shared memory (another process wanted the card) stays there
        # once the pressure is gone, and every token then crawls over PCIe. When the card has
        # room for it again, this restarts the runtime with the same model so it loads resident.
        self.on_paged_model_reload = on_paged_model_reload
        self.paged_reload_reserve_mb = paged_reload_reserve_mb
        self._paged_streak = 0
        self._last_paged_reload = 0.0
        self._paged_reload_task: Optional[asyncio.Task] = None
        # Whether our own llama-server is decoding right now (RuntimeProcessManager.is_processing,
        # from GET /slots). Only requests that come through the orchestrator register an activity;
        # one sent straight to the server's port -- a curl replay, a script, another client --
        # drove the GPU to 98% with nothing registered, which read as a game and evicted the
        # model after the debounce. Compute from our own child is never an external workload.
        # VRAM pressure from *other* processes is judged separately and still counts.
        self.runtime_busy_provider = runtime_busy_provider
        self._runtime_busy = False
        # How much of the card is Jarvis's own model (RuntimeProcessManager.model_vram_mb): the
        # governor must never evict the model to "yield" VRAM that the model itself is using.
        self.model_vram_mb_provider = model_vram_mb_provider
        self.external_vram_floor_mb = external_vram_floor_mb
        # What the desktop already held when the model was launched. An absolute floor cannot
        # tell "this user's ordinary desktop is 2.3 GB" from "a game just started": the first
        # was read as the second and evicted the model on every poll. Growth *beyond* the
        # baseline is what says something new wants the card.
        self.external_vram_baseline_provider = external_vram_baseline_provider
        self.external_vram_growth_mb = external_vram_growth_mb
        # (baseline the provider reported, lowest external share seen since it did): the
        # baseline follows the desktop *down* -- close Steam and later start a game, and the
        # game is measured against the quieter desktop, not the one from launch time.
        self._external_baseline: Optional[tuple[float, float]] = None
        self.poll_interval = poll_interval
        self.gpu_threshold = gpu_threshold
        self.vram_threshold = vram_threshold
        self.cpu_threshold = cpu_threshold
        self.ram_threshold = ram_threshold
        self.sustained_breach_polls = sustained_breach_polls
        self.recovery_polls = recovery_polls
        self.auto_unload_on_throttle = auto_unload_on_throttle
        self.on_throttle_unload = on_throttle_unload
        self.on_reload = on_reload
        self.startup_grace_seconds = startup_grace_seconds

        self._running = False
        self._poll_task: Optional[asyncio.Task] = None
        self._nvml_initialized = False
        self._nvml_handle: Any = None
        self._current_metrics = SystemMetrics()
        self._lock = asyncio.Lock()
        self._start_time = time.time()
        self._error_state = False
        self._error_reason: Optional[str] = None
        self._in_status_transition = False

        # 1. Activity Registry
        self._active_activities: dict[str, ActivityRecord] = {}

        # 2. Debounce & Hysteresis State
        self._breach_streak = 0
        self._recovery_streak = 0
        self._debounced_throttled = False
        self._debounced_reasons: list[str] = []
        self._throttle_streak = 0

        # 3. Expiry-Scoped Unload Reason Tracker (Fixes Bug 2 & 6)
        self._unload_reasons: dict[str, UnloadReason] = {}
        self._pending_reload = False

        # 4. Manual Overrides
        self._manual_paused = False
        self._manual_pause_reason: Optional[str] = None
        self._manual_resume_override_until: Optional[float] = None

        # 5. External App Watcher State
        self._external_apps_active: dict[str, float] = {}
        self._pending_external_app_unloads: set[str] = set()

        # 6. Observability Ring Buffer
        self._history: deque[GovernorEvent] = deque(maxlen=50)
        self._last_status: str = GovernorStatus.IDLE.value

        # 7. What the rest of the machine has held on the card lately, for the model picker's
        # fit labels: (timestamp, external MiB, card total MiB), one per collect_metrics.
        self._external_samples: deque[tuple[float, float, float]] = deque(maxlen=2000)

        self._init_nvml()

        # Prime psutil CPU percent calculation
        try:
            psutil.cpu_percent(interval=None)
        except Exception:
            pass

    # --- 1. Unload Reason Tracker Helpers ---

    def _add_unload_reason(
        self,
        reason_id: str,
        source: str,
        label: str,
        duration_seconds: Optional[float] = None
    ) -> None:
        expires_at = (time.time() + duration_seconds) if duration_seconds is not None else None
        self._unload_reasons[reason_id] = UnloadReason(
            reason_id=reason_id,
            source=source,
            label=label,
            created_at=time.time(),
            expires_at=expires_at
        )

    def _remove_unload_reason(self, reason_id: str) -> None:
        self._unload_reasons.pop(reason_id, None)

    def _cleanup_expired_unload_reasons(self, trigger_transition: bool = True) -> list[UnloadReason]:
        """
        Cleans up expired reasons (Bug 7 fix). Re-evaluates status and schedules
        automatic reload if all reasons have expired.
        """
        now = time.time()
        expired: list[UnloadReason] = []
        for k, r in list(self._unload_reasons.items()):
            if r.expires_at is not None and now >= r.expires_at:
                expired.append(self._unload_reasons.pop(k))

        if expired and trigger_transition and not self._in_status_transition:
            expired_reasons_str = [f"unload reason expired: {r.reason_id}" for r in expired]
            logger.info("Governor: Purged %d expired unload reason(s): %s", len(expired), expired_reasons_str)
            if not self._unload_reasons and not self._external_apps_active:
                self._pending_reload = True
                if self.on_reload:
                    self._trigger_reload_callback(vram_settle_delay_seconds=0.0)
            self._check_status_transition(trigger_reasons=expired_reasons_str)

        return expired

    # --- 2. Activity Registry API ---

    def begin_activity(
        self, activity_type: Union[ActivityType, str], label: Optional[str] = None
    ) -> str:
        """
        Registers an active Jarvis activity, returns an activity_id.
        Resets the auto-unload throttle streak.
        """
        act_enum = ActivityType(activity_type) if isinstance(activity_type, str) else activity_type
        activity_id = f"act_{uuid.uuid4().hex[:8]}"
        record = ActivityRecord(
            activity_id=activity_id,
            activity_type=act_enum,
            label=label,
            started_at=time.time()
        )
        self._active_activities[activity_id] = record
        self._throttle_streak = 0
        logger.debug("Governor activity begun: %s (%s) [ID: %s]", act_enum.value, label, activity_id)
        label_str = f" ({label})" if label else ""
        self._check_status_transition(trigger_reasons=[f"activity begun: {act_enum.value}{label_str}"])
        return activity_id

    def end_activity(self, activity_id: str) -> None:
        """
        Removes the registered activity. Applies any deferred external app unloads when idle.
        """
        removed = self._active_activities.pop(activity_id, None)
        if removed:
            logger.debug("Governor activity ended: %s [ID: %s]", removed.activity_type.value, activity_id)
            
            # If all Jarvis activities have concluded and pending external app unloads exist, trigger unload
            if not self.is_busy and self._pending_external_app_unloads:
                apps_list = list(self._pending_external_app_unloads)
                for app_lbl in apps_list:
                    self._add_unload_reason(
                        reason_id=f"external_app:{app_lbl}",
                        source="external_app",
                        label=app_lbl
                    )
                self._pending_external_app_unloads.clear()
                logger.info(
                    "Governor: Applying deferred external app auto-unload for %s now that Jarvis is idle.",
                    ", ".join(apps_list)
                )
                self._trigger_unload_callback()

            label_str = f" ({removed.label})" if removed.label else ""
            self._check_status_transition(trigger_reasons=[f"activity ended: {removed.activity_type.value}{label_str}"])

    def activity(
        self, activity_type: Union[ActivityType, str], label: Optional[str] = None
    ) -> "_ActivityContext":
        """
        Async context manager convenience wrapper:
            async with governor.activity(ActivityType.INFERENCING, label="chat"):
                ...
        """
        return _ActivityContext(self, activity_type, label)

    # Backward compatibility wrappers
    def set_inferencing(self, state: bool) -> None:
        """Deprecated: Prefer `async with governor.activity(ActivityType.INFERENCING):`"""
        fixed_id = "legacy_inferencing_fixed_id"
        if state:
            self._active_activities[fixed_id] = ActivityRecord(
                activity_id=fixed_id,
                activity_type=ActivityType.INFERENCING,
                label="legacy_inference"
            )
            self._throttle_streak = 0
            self._check_status_transition(trigger_reasons=["activity begun: inferencing (legacy)"])
        else:
            self._active_activities.pop(fixed_id, None)
            self._check_status_transition(trigger_reasons=["activity ended: inferencing (legacy)"])

    def set_loading_model(self, state: bool) -> None:
        """Deprecated: Prefer `async with governor.activity(ActivityType.MODEL_LOADING):`"""
        fixed_id = "legacy_loading_fixed_id"
        if state:
            self._active_activities[fixed_id] = ActivityRecord(
                activity_id=fixed_id,
                activity_type=ActivityType.MODEL_LOADING,
                label="legacy_loading"
            )
            self._throttle_streak = 0
            self._check_status_transition(trigger_reasons=["activity begun: model_loading (legacy)"])
        else:
            self._active_activities.pop(fixed_id, None)
            self._check_status_transition(trigger_reasons=["activity ended: model_loading (legacy)"])

    def loading_model(self) -> "_ActivityContext":
        """Deprecated: Prefer `governor.activity(ActivityType.MODEL_LOADING)`"""
        return self.activity(ActivityType.MODEL_LOADING, label="model_loading")

    # --- 3. Properties ---

    @property
    def is_busy(self) -> bool:
        """True if any Jarvis activity is currently registered."""
        return len(self._active_activities) > 0

    @property
    def active_activities(self) -> list[ActivityRecord]:
        return list(self._active_activities.values())

    @property
    def active_activity_types(self) -> list[str]:
        return [a.activity_type.value for a in self._active_activities.values()]

    @property
    def is_inferencing(self) -> bool:
        return any(a.activity_type == ActivityType.INFERENCING for a in self._active_activities.values())

    @property
    def is_loading_model(self) -> bool:
        return any(a.activity_type == ActivityType.MODEL_LOADING for a in self._active_activities.values())

    @property
    def in_startup_grace(self) -> bool:
        return (time.time() - self._start_time) < self.startup_grace_seconds

    @property
    def model_unloaded(self) -> bool:
        """Returns True if any active, unexpired unload reason exists."""
        self._cleanup_expired_unload_reasons(trigger_transition=False)
        return len(self._unload_reasons) > 0

    @property
    def pending_reload(self) -> bool:
        """Returns True if model was unloaded and is awaiting reload."""
        return self._pending_reload

    @property
    def override_expires_at(self) -> Optional[float]:
        """Returns the unix timestamp when timed override expires, or None if not active."""
        if self._is_resume_override_active and self._manual_resume_override_until != float("inf"):
            return self._manual_resume_override_until
        return None

    @property
    def is_manual_override(self) -> bool:
        """Returns True only when a timed override is currently active (not expired)."""
        return self._is_resume_override_active

    @property
    def manual_override_active(self) -> bool:
        """Returns True if manual pause or timed override is active."""
        return self._manual_paused or self._is_resume_override_active

    @property
    def _is_resume_override_active(self) -> bool:
        if self._manual_resume_override_until is None:
            return False
        if time.time() < self._manual_resume_override_until:
            return True
        # Timed override has elapsed (Bug 4 fix)
        self._manual_resume_override_until = None
        logger.info("Governor resume override expired. Triggering reload callback and reverting state.")
        self._trigger_reload_callback(vram_settle_delay_seconds=0.0)
        self._check_status_transition(trigger_reasons=["manual resume override expired"])
        return False

    @property
    def status(self) -> GovernorStatus:
        """
        Computed 6-tier status hierarchy:
        1. ERROR -> ERROR
        2. manual_paused -> PAUSED
        3. _is_resume_override_active -> RUNNING (if busy) else IDLE
        4. external_apps_active -> PAUSED
        5. is_busy and is_loading_model -> LOADING
        6. is_busy -> RUNNING
        7. (debounced_throttled or model_unloaded) -> UNLOADED
        8. else -> IDLE
        """
        if self._error_state:
            return GovernorStatus.ERROR
        if self._manual_paused:
            return GovernorStatus.PAUSED
        if self._is_resume_override_active:
            if self.is_busy:
                if self.is_loading_model:
                    return GovernorStatus.LOADING
                return GovernorStatus.RUNNING
            return GovernorStatus.IDLE
        if bool(self._external_apps_active):
            return GovernorStatus.PAUSED
        if self.is_busy:
            if self.is_loading_model:
                return GovernorStatus.LOADING
            return GovernorStatus.RUNNING
        if self.model_unloaded or self._debounced_throttled:
            return GovernorStatus.UNLOADED
        return GovernorStatus.IDLE

    # --- 4. Manual Overrides ---

    def force_pause(self, reason: str = "manual override") -> None:
        """Forces governor into PAUSED state. Blocks wait_until_healthy."""
        self._manual_paused = True
        self._manual_pause_reason = reason
        self._manual_resume_override_until = None
        logger.info("Governor manually PAUSED (reason: %s)", reason)
        self._check_status_transition(trigger_reasons=[f"manual pause: {reason}"])

    def force_resume(self) -> None:
        """Clears manual pause and override states, restoring automated governance."""
        self._manual_paused = False
        self._manual_pause_reason = None
        self._manual_resume_override_until = None
        logger.info("Governor manually RESUMED (automatic mode restored)")
        self._check_status_transition(trigger_reasons=["manual resume: restored automated governance"])

    def clear_error(self) -> None:
        """Clears governor error state, restoring normal governance."""
        self._error_state = False
        self._error_reason = None
        logger.info("Governor error state cleared manually.")
        self._check_status_transition(trigger_reasons=["error cleared manually"])

    def force_reload(self) -> bool:
        """
        Manually triggers model reload into GPU VRAM.
        Returns True if reload was initiated, False if model is already resident / no-op.
        """
        if not self.model_unloaded and not self.pending_reload and not self._error_state:
            logger.info("Governor force_reload ignored: Model is already loaded and resident in VRAM.")
            return False
        logger.info("Governor force_reload invoked manually.")
        self._pending_reload = True
        settle_delay = 0.5 if bool(self._external_apps_active) else 0.1
        self._trigger_reload_callback(vram_settle_delay_seconds=settle_delay)
        return True

    def force_resume_ignore_metrics(self, duration_seconds: Optional[float] = None) -> None:
        """
        Forces governor to report healthy/IDLE regardless of load.
        duration_seconds: None for indefinite until cleared, or float seconds.
        """
        self._manual_paused = False
        self._manual_pause_reason = None
        if duration_seconds is not None:
            self._manual_resume_override_until = time.time() + float(duration_seconds)
            logger.info("Governor override active: Ignoring metrics for %.1f seconds", duration_seconds)
            self._check_status_transition(trigger_reasons=[f"manual resume override (duration={duration_seconds}s)"])
        else:
            self._manual_resume_override_until = float("inf")
            logger.info("Governor override active: Ignoring metrics indefinitely")
            self._check_status_transition(trigger_reasons=["manual resume override (indefinite)"])

    # --- 5. External Process Watcher Integration ---

    def report_external_app(self, label: str, present: bool) -> None:
        """
        Called by ProcessWatcher on confirmed launch or close of heavy 3D/editing apps.
        Multi-app presence isolation (Bug 6 fix).
        """
        if present:
            self._external_apps_active[label] = time.time()
            logger.warning("Governor notified: Heavy external app active: %s", label)
            if self.is_busy:
                self._pending_external_app_unloads.add(label)
                logger.info("Governor: Deferring auto-unload for '%s' until current Jarvis turn ends.", label)
            else:
                already_unloaded = self.model_unloaded
                self._add_unload_reason(
                    reason_id=f"external_app:{label}",
                    source="external_app",
                    label=label
                )
                if self.auto_unload_on_throttle and not already_unloaded:
                    self._trigger_unload_callback()
            self._check_status_transition(trigger_reasons=[f"external app detected: {label}"])
        else:
            self._external_apps_active.pop(label, None)
            self._pending_external_app_unloads.discard(label)
            self._remove_unload_reason(f"external_app:{label}")
            logger.info("Governor notified: External app closed: %s", label)
            if not self._external_apps_active:
                if not self.model_unloaded:
                    self._pending_reload = True
                    if self.on_reload:
                        self._trigger_reload_callback(vram_settle_delay_seconds=0.5)
                self._check_status_transition(trigger_reasons=[f"external app closed: {label}"])
            else:
                remaining = ", ".join(sorted(self._external_apps_active.keys()))
                logger.info("Governor: External app %s closed, but other external apps still running (%s). Remaining PAUSED.", label, remaining)
                self._check_status_transition(trigger_reasons=[f"external app closed: {label} (active: {remaining})"])

    def _trigger_unload_callback(self) -> None:
        """Executes the on_throttle_unload callback to evict models from VRAM."""
        if not self.on_throttle_unload:
            return

        async def _execute_unload():
            try:
                logger.warning("Governor AUTO-UNLOAD: Evicting models from GPU VRAM to yield to external workload.")
                if asyncio.iscoroutinefunction(self.on_throttle_unload):
                    res = await self.on_throttle_unload()
                else:
                    res = self.on_throttle_unload()

                if res is not False:
                    logger.info("Governor: Model eviction callback completed successfully.")
                else:
                    logger.warning("Governor: Model eviction callback reported partial or failed unload.")
            except Exception as e:
                logger.error("Error executing governor unload callback: %s", e)

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_execute_unload())
        except RuntimeError:
            asyncio.run(_execute_unload())

    def _trigger_reload_callback(self, vram_settle_delay_seconds: float = 0.5) -> None:
        """
        Executes the on_reload callback to reload models into VRAM with settle delay,
        exponential backoff retries, and clean error state transitions (Bugs 3, 5, 8).
        """
        if not self.on_reload:
            self._pending_reload = False
            return

        async def _execute_reload():
            max_retries = 3
            backoff = 0.5
            for attempt in range(1, max_retries + 1):
                try:
                    if attempt == 1 and vram_settle_delay_seconds > 0:
                        await asyncio.sleep(vram_settle_delay_seconds)
                    elif attempt > 1:
                        await asyncio.sleep(backoff)
                        backoff *= 2.0

                    logger.info("Governor RELOAD: Reloading models into GPU VRAM (attempt %d/%d)...", attempt, max_retries)
                    if asyncio.iscoroutinefunction(self.on_reload):
                        res = await self.on_reload()
                    else:
                        res = self.on_reload()

                    if res is not False:
                        self._pending_reload = False
                        self._error_state = False
                        self._error_reason = None
                        logger.info("Governor: Model reload callback completed successfully.")
                        return
                    else:
                        logger.warning("Governor: Model reload attempt %d/%d returned failure.", attempt, max_retries)
                except Exception as e:
                    logger.warning("Governor: Model reload attempt %d/%d error: %s", attempt, max_retries, e)

            # Max retries exhausted -> clear _pending_reload and transition to ERROR state
            self._pending_reload = False
            self._error_state = True
            self._error_reason = f"Model reload failed after {max_retries} attempts"
            logger.error("Governor: Model reload permanently failed after %d retries. Transitioning to ERROR state.", max_retries)
            self._check_status_transition(trigger_reasons=["model reload failed after maximum retries"])

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_execute_reload())
        except RuntimeError:
            asyncio.run(_execute_reload())

    # --- 6. Observability: Status History Ring Buffer ---

    def _check_status_transition(
        self,
        metrics: Optional[SystemMetrics] = None,
        trigger_reasons: Optional[list[str]] = None
    ) -> None:
        """
        Evaluates current status and records a GovernorEvent if status transitioned.
        Recursion-safe and context-preserving (Bug 4 & 7 fix).
        """
        if self._in_status_transition:
            return
        self._in_status_transition = True
        try:
            current_status_val = self.status.value
            if current_status_val != self._last_status:
                from_st = self._last_status
                self._last_status = current_status_val

                if trigger_reasons is not None:
                    reasons = list(trigger_reasons)
                elif self._error_state:
                    reasons = [f"governor error: {self._error_reason or 'unknown failure'}"]
                elif self._manual_paused:
                    reasons = [f"manual pause: {self._manual_pause_reason or 'manual override'}"]
                elif self._external_apps_active:
                    reasons = [f"external app detected: {k}" for k in sorted(self._external_apps_active.keys())]
                elif self._debounced_throttled or (metrics and metrics.throttled):
                    reasons = list(metrics.throttle_reasons if metrics else self._debounced_reasons)
                elif self.is_busy:
                    reasons = [f"activity: {a.activity_type.value}" for a in self.active_activities]
                else:
                    reasons = []

                event = GovernorEvent(
                    timestamp=time.time(),
                    from_status=from_st,
                    to_status=current_status_val,
                    raw_reasons=reasons,
                    metrics_snapshot=metrics or self._current_metrics,
                    active_activities=self.active_activity_types
                )
                self._history.append(event)
                logger.info("Governor status transition: %s -> %s (reasons: %s)", from_st, current_status_val, reasons)
        finally:
            self._in_status_transition = False

    def get_history(self, limit: int = 50) -> list[GovernorEvent]:
        """
        Returns recent status transition events (most recent first).
        """
        items = list(self._history)
        items.reverse()
        return items[:limit]

    # --- 7. Telemetry Collection & Debouncing Engine ---

    def _init_nvml(self) -> None:
        if not HAS_NVML:
            logger.info("PyNVML not installed. GPU hardware metrics will be disabled.")
            return

        try:
            pynvml.nvmlInit()
            device_count = pynvml.nvmlDeviceGetCount()
            if device_count > 0:
                self._nvml_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
                self._nvml_initialized = True
                name = pynvml.nvmlDeviceGetName(self._nvml_handle)
                logger.info("Initialized NVML for GPU: %s", name)
        except Exception as e:
            logger.warning("Failed to initialize NVML: %s. Continuing with CPU/RAM metrics only.", e)
            self._nvml_initialized = False

    def _track_external_baseline(self, metrics: SystemMetrics) -> Optional[float]:
        """The external share to measure growth against right now, or None when unknown."""
        if metrics.model_vram_mb <= 0 or self.external_vram_baseline_provider is None:
            self._external_baseline = None
            return None
        try:
            reported = self.external_vram_baseline_provider()
        except Exception as exc:
            logger.debug("external VRAM baseline provider failed: %s", exc)
            reported = None
        if reported is None:
            self._external_baseline = None
            return None
        reported = float(reported)
        if self._external_baseline is None or self._external_baseline[0] != reported:
            # A new launch (or the first one): start tracking from what it measured.
            self._external_baseline = (reported, reported)
        if metrics.model_resident:
            # Only a card that holds the whole model gives a real external figure. While part
            # of the model sits in shared memory the subtraction reads ~0, and latching the
            # baseline onto that would turn the desktop's ordinary return into "growth".
            lowest = min(self._external_baseline[1], metrics.external_vram_mb)
            self._external_baseline = (reported, lowest)
        return round(self._external_baseline[1], 1)

    def vram_headroom_mb(self, reserve_mb: float = 0.0) -> Optional[float]:
        """
        MiB the card can still give a launch: physically free minus a reserve for the desktop
        to breathe. None when NVML cannot say -- the caller then launches and lets a failure
        decide. Deliberately not the eviction threshold: a launch that lands at 90% is fine,
        the threshold exists to notice *growth* afterwards.
        """
        metrics = self.collect_metrics()
        if not metrics.gpu_available or metrics.vram_total_mb <= 0:
            return None
        return round(max(0.0, metrics.vram_free_mb - reserve_mb), 1)

    def fit_budget_mb(self, window_s: float = 600.0) -> Optional[float]:
        """
        MiB of the card a Jarvis model can count on: the total minus the most the rest of the
        machine has held over the last ``window_s`` (a high-water mark, so the desktop's swings
        between ~1.1 and ~2.3 GB don't flip a model between "fits" and "spills" as a browser
        tab opens). None before the first GPU sample.
        """
        if not self._external_samples:
            return None
        newest = self._external_samples[-1][0]
        recent = [s for s in self._external_samples if s[0] >= newest - window_s]
        total = recent[-1][2]
        return round(max(0.0, total - max(s[1] for s in recent)), 1)

    def card_total_mb(self) -> Optional[float]:
        """The card's total VRAM from the latest GPU sample, or None before the first."""
        return self._external_samples[-1][2] if self._external_samples else None

    def _vram_reasons(self, metrics: SystemMetrics) -> list[str]:
        """
        VRAM is only a reason to evict when something *other than the model* needs the card.

        The 9B with its projector and a 32k context sits at ~6 GB of the 4060's 8 GB, and an
        ordinary desktop (compositor, wallpaper engine, a browser) adds 1-2 GB more: 96-97%
        utilisation is the steady state of a working session, not pressure. The previous rule
        read "VRAM >= 92% and GPU busy >= 30%" as an external workload and evicted the model
        between two chat messages, then watched the same 96% persist -- because the usage was
        the model. So the rule looks at the external share (total minus what the runtime manager
        says its model costs): evict when that share grows past the floor (a game, a renderer),
        or when the card is at its hardware ceiling and the model itself is about to page.

        The floor itself is relative when it can be: the user's ordinary desktop (Steam, Discord,
        a browser) sits at 2.3 GB, above any absolute floor that would still catch a game on a
        quiet desktop. So the share is compared with what it was when the model launched, and
        only growth beyond ``external_vram_growth_mb`` counts. The absolute floor remains for a
        launch whose baseline is unknown.
        """
        if not metrics.gpu_available or metrics.vram_util_percent < self.vram_threshold:
            return []
        if metrics.vram_util_percent >= 99.0:
            return [
                f"VRAM at hardware ceiling ({metrics.vram_util_percent}%, "
                f"{metrics.external_vram_mb:.0f} MiB external beyond the model's {metrics.model_vram_mb:.0f} MiB)"
            ]
        if metrics.model_vram_mb > 0 and metrics.external_vram_baseline_mb is not None:
            growth = metrics.external_vram_mb - metrics.external_vram_baseline_mb
            if growth >= self.external_vram_growth_mb:
                return [
                    f"External VRAM use grew {growth:.0f} MiB beyond the {metrics.external_vram_baseline_mb:.0f} MiB "
                    f"the desktop held at launch (limit {self.external_vram_growth_mb:.0f} MiB) "
                    f"at {metrics.vram_util_percent}% utilization"
                ]
            return []
        if metrics.model_vram_mb > 0 and metrics.external_vram_mb >= self.external_vram_floor_mb:
            # No baseline for this launch (adopted server, NVML hiccup): the absolute floor is
            # all there is to go on.
            return [
                f"External VRAM use ({metrics.external_vram_mb:.0f} MiB beyond the model's "
                f"{metrics.model_vram_mb:.0f} MiB) exceeds floor ({self.external_vram_floor_mb:.0f} MiB) "
                f"at {metrics.vram_util_percent}% utilization"
            ]
        if metrics.model_vram_mb <= 0 and metrics.gpu_util_percent >= 30.0:
            # No model of ours is loaded, so all of it is external: the old coupling still applies.
            return [
                f"VRAM utilization ({metrics.vram_util_percent}%) exceeds threshold ({self.vram_threshold}%) under external load"
            ]
        return []

    def collect_metrics(self) -> SystemMetrics:
        """
        Poll real-time hardware telemetry and evaluate threshold breaches.
        """
        metrics = SystemMetrics(timestamp=time.time())

        # 1. CPU & RAM Telemetry
        try:
            metrics.cpu_percent = psutil.cpu_percent(interval=None)
            ram = psutil.virtual_memory()
            metrics.ram_percent = ram.percent
            metrics.ram_used_mb = round(ram.used / (1024 * 1024), 1)
            metrics.ram_total_mb = round(ram.total / (1024 * 1024), 1)
        except Exception as e:
            logger.error("Error reading CPU/RAM metrics: %s", e)

        # 2. GPU & VRAM Telemetry
        if self._nvml_initialized and self._nvml_handle:
            try:
                metrics.gpu_available = True
                metrics.gpu_name = pynvml.nvmlDeviceGetName(self._nvml_handle)

                # GPU Core Utilization
                util_rates = pynvml.nvmlDeviceGetUtilizationRates(self._nvml_handle)
                metrics.gpu_util_percent = float(util_rates.gpu)

                # VRAM Usage
                mem_info = pynvml.nvmlDeviceGetMemoryInfo(self._nvml_handle)
                metrics.vram_total_mb = round(mem_info.total / (1024 * 1024), 1)
                metrics.vram_used_mb = round(mem_info.used / (1024 * 1024), 1)
                metrics.vram_free_mb = round(mem_info.free / (1024 * 1024), 1)
                if mem_info.total > 0:
                    metrics.vram_util_percent = round((mem_info.used / mem_info.total) * 100.0, 1)
                if self.model_vram_mb_provider is not None:
                    try:
                        metrics.model_vram_mb = float(self.model_vram_mb_provider() or 0.0)
                    except Exception as exc:
                        logger.debug("model VRAM provider failed: %s", exc)
                metrics.external_vram_mb = round(max(0.0, metrics.vram_used_mb - metrics.model_vram_mb), 1)
                metrics.model_resident = metrics.model_vram_mb <= 0 or metrics.vram_used_mb >= metrics.model_vram_mb
                metrics.external_vram_baseline_mb = self._track_external_baseline(metrics)
                if metrics.model_resident:
                    # A paged-out model makes the subtraction read ~0 external; skip those.
                    self._external_samples.append(
                        (metrics.timestamp, metrics.external_vram_mb, metrics.vram_total_mb)
                    )

                # GPU Temperature
                try:
                    metrics.gpu_temp_c = float(pynvml.nvmlDeviceGetTemperature(self._nvml_handle, pynvml.NVML_TEMPERATURE_GPU))
                except Exception:
                    pass

            except Exception as e:
                logger.warning("Error reading GPU metrics: %s", e)

        # 3. Evaluate Instantaneous (Raw) Threshold Breaches
        reasons = []
        if self.enabled and not self.is_busy:
            # External GPU Compute Load (games, 3D renderers)
            if metrics.gpu_available and metrics.gpu_util_percent >= self.gpu_threshold:
                if self._runtime_busy:
                    logger.debug(
                        "GPU at %.0f%% but llama-server is processing an unregistered request; not external load",
                        metrics.gpu_util_percent,
                    )
                else:
                    reasons.append(
                        f"GPU compute utilization ({metrics.gpu_util_percent}%) exceeds threshold ({self.gpu_threshold}%)"
                    )

            reasons.extend(self._vram_reasons(metrics))

            if metrics.cpu_percent >= self.cpu_threshold:
                reasons.append(
                    f"CPU utilization ({metrics.cpu_percent}%) exceeds threshold ({self.cpu_threshold}%)"
                )
            if metrics.ram_percent >= self.ram_threshold:
                reasons.append(
                    f"System RAM utilization ({metrics.ram_percent}%) exceeds threshold ({self.ram_threshold}%)"
                )

        metrics.raw_throttled = len(reasons) > 0

        # 4. Apply Hysteresis / Debounce
        if metrics.raw_throttled:
            self._recovery_streak = 0
            self._breach_streak += 1
            if self._breach_streak >= self.sustained_breach_polls:
                self._debounced_throttled = True
                self._debounced_reasons = list(reasons)
        else:
            self._breach_streak = 0
            self._recovery_streak += 1
            if self._recovery_streak >= self.recovery_polls:
                self._debounced_throttled = False
                self._debounced_reasons = []

        metrics.throttled = self._debounced_throttled
        metrics.throttle_reasons = list(self._debounced_reasons)

        return metrics

    async def _probe_runtime_busy(self) -> bool:
        """True only when the provider positively reports our llama-server decoding; unknown
        (no provider, nothing running, endpoint unreachable) is treated as not busy so the
        external-compute rule keeps working for an adopted or absent server."""
        if self.runtime_busy_provider is None:
            return False
        try:
            return bool(await self.runtime_busy_provider())
        except Exception as exc:
            logger.debug("runtime busy probe failed: %s", exc)
            return False

    async def _poll_loop(self) -> None:
        logger.info(
            "Governor V2 polling loop started (interval=%.1fs, breach_debounce=%dp, recovery_debounce=%dp)",
            self.poll_interval,
            self.sustained_breach_polls,
            self.recovery_polls
        )
        while self._running:
            try:
                # 1. Live check timed override expiration
                if self._manual_resume_override_until is not None and time.time() >= self._manual_resume_override_until:
                    logger.info("Governor timed override expired. Reverting to automatic governance.")
                    self._manual_resume_override_until = None
                    if self.model_unloaded or self._pending_reload:
                        self._trigger_reload_callback()

                # 2. Clean up any expired unload reasons
                self._cleanup_expired_unload_reasons()

                # 3. Collect metrics (asking the runtime first whether the compute is its own)
                self._runtime_busy = await self._probe_runtime_busy()
                metrics = self.collect_metrics()
                async with self._lock:
                    self._current_metrics = metrics

                self._check_status_transition(metrics)

                if self._paged_reload_due(metrics):
                    self._start_paged_reload(metrics)

                # 4. Auto-unload logic: count consecutive DEBOUNCED throttled polls while idle
                if self.is_busy or self.in_startup_grace or self._is_resume_override_active:
                    self._throttle_streak = 0
                elif self._debounced_throttled:
                    self._throttle_streak += 1
                    logger.warning(
                        "Governor DEBOUNCED THROTTLE (streak=%d): %s",
                        self._throttle_streak,
                        "; ".join(self._debounced_reasons)
                    )

                    # Trigger auto-unload on sustained GPU/VRAM/External app throttle
                    has_gpu_reason = any(
                        "GPU" in r or "VRAM" in r or "external app" in r
                        for r in self._debounced_reasons
                    )
                    if (
                        self.auto_unload_on_throttle
                        and self._throttle_streak >= 4
                        and not self.model_unloaded
                        and not self.is_busy
                        and has_gpu_reason
                    ):
                        self._add_unload_reason(
                            reason_id="metrics_throttle",
                            source="metrics",
                            label="metrics_throttle"
                        )
                        self._trigger_unload_callback()
                else:
                    self._throttle_streak = 0
                    # Disjoint recovery: Only clear metrics unload reason, never external apps
                    if "metrics_throttle" in self._unload_reasons:
                        logger.info("Resource Governor: External metrics load returned to normal.")
                        self._remove_unload_reason("metrics_throttle")
                        if not self.model_unloaded:
                            self._pending_reload = True
                            if self.on_reload:
                                self._trigger_reload_callback(vram_settle_delay_seconds=0.5)
                        self._check_status_transition(metrics, trigger_reasons=["metrics load recovered below thresholds"])

            except Exception as e:
                logger.error("Unexpected error in governor poll loop: %s", e)

            await asyncio.sleep(self.poll_interval)

    # Polls the model must stay paged out before a reload is considered (a brief dip in the
    # reading is not a demotion), and the least time between two reloads: if the card fills
    # again at once, the second demotion waits instead of looping.
    PAGED_SETTLE_POLLS = 3
    PAGED_RELOAD_COOLDOWN_S = 60.0

    def _paged_reload_due(self, metrics: SystemMetrics) -> bool:
        """
        True when our model has sat partly in shared memory for a few polls and the card now
        has room for the part that is missing: whatever pushed it out has let go. The part still
        on the card is estimated against the desktop's share from before the pressure (the live
        external share is unknowable while the model is paged); with no such baseline, room for
        the whole model is required.
        """
        if not metrics.gpu_available or metrics.model_vram_mb <= 0 or metrics.model_resident:
            self._paged_streak = 0
            return False
        self._paged_streak += 1
        if self.on_paged_model_reload is None or self._paged_streak < self.PAGED_SETTLE_POLLS:
            return False
        if (
            self.is_busy
            or self._runtime_busy
            or self.in_startup_grace
            or self._manual_paused
            or self._debounced_throttled
            or (self._paged_reload_task is not None and not self._paged_reload_task.done())
            or time.time() - self._last_paged_reload < self.PAGED_RELOAD_COOLDOWN_S
        ):
            return False
        baseline = metrics.external_vram_baseline_mb
        if baseline is not None:
            on_card = max(0.0, metrics.vram_used_mb - baseline)
            missing = max(0.0, metrics.model_vram_mb - on_card)
        else:
            missing = metrics.model_vram_mb
        return metrics.vram_free_mb >= missing + self.paged_reload_reserve_mb

    def _start_paged_reload(self, metrics: SystemMetrics) -> None:
        self._last_paged_reload = time.time()
        self._paged_streak = 0
        logger.warning(
            "Model paged out of VRAM (%.0f MiB used on a card with %.0f MiB free; model %.0f MiB): "
            "the pressure has passed, reloading it resident.",
            metrics.vram_used_mb, metrics.vram_free_mb, metrics.model_vram_mb,
        )

        async def _run() -> None:
            try:
                async with self.activity(ActivityType.MODEL_LOADING, label="paged-out reload"):
                    ok = await self.on_paged_model_reload()
                logger.info("Paged-out model reload %s.", "succeeded" if ok else "failed")
            except Exception as exc:
                logger.warning("Paged-out model reload failed: %s", exc)

        self._paged_reload_task = asyncio.create_task(_run())

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._start_time = time.time()
        self._current_metrics = self.collect_metrics()
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None

        if self._nvml_initialized:
            try:
                pynvml.nvmlShutdown()
                self._nvml_initialized = False
            except Exception:
                pass
        logger.info("Resource Governor stopped.")

    async def get_metrics(self) -> SystemMetrics:
        async with self._lock:
            return self._current_metrics

    async def is_throttled(self) -> tuple[bool, Optional[str]]:
        """
        Returns (is_throttled, primary_reason) considering manual overrides,
        external apps, and debounced telemetry.
        """
        if self._manual_paused:
            return True, self._manual_pause_reason or "Manual pause active"
        if self._external_apps_active:
            return True, f"External app running: {list(self._external_apps_active.keys())[0]}"
        if self.is_busy or self._is_resume_override_active:
            return False, None
        
        metrics = await self.get_metrics()
        if metrics.throttled and metrics.throttle_reasons:
            return True, metrics.throttle_reasons[0]
        return False, None

    async def wait_until_healthy(self, timeout_seconds: float = 3.0) -> tuple[bool, Optional[str]]:
        """
        Adaptive queueing: Waits up to timeout_seconds for temporary load spikes or pause to clear.
        Returns (is_healthy, throttle_reason).
        """
        if self.is_busy or self._is_resume_override_active:
            return True, None

        throttled, reason = await self.is_throttled()
        if not throttled:
            return True, None

        logger.info("Governor queueing: Waiting up to %.1fs for system load/pause to clear (%s)...", timeout_seconds, reason)
        start_time = time.time()

        while time.time() - start_time < timeout_seconds:
            await asyncio.sleep(0.25)
            if self.is_busy or self._is_resume_override_active:
                return True, None
            throttled, reason = await self.is_throttled()
            if not throttled:
                logger.info("System load cleared. Resuming request.")
                return True, None

        return False, reason

    def can_allocate_vram(self, required_mb: float, live_poll: bool = True) -> tuple[bool, str]:
        """
        Evaluates whether required_mb of additional GPU VRAM can be safely allocated
        without breaching the configured VRAM threshold or running out of physical headroom.
        Returns (can_allocate: bool, reason: str).
        """
        if not self.enabled:
            return True, "Governor disabled"
        if self._manual_paused:
            return False, f"Manual pause active: {self._manual_pause_reason or 'paused'}"
        if self._external_apps_active:
            apps = ", ".join(sorted(self._external_apps_active.keys()))
            return False, f"External heavy app active ({apps})"
        if self._debounced_throttled:
            reasons = "; ".join(self._debounced_reasons) if self._debounced_reasons else "Host throttled"
            return False, f"Governor throttled: {reasons}"

        metrics = self.collect_metrics() if live_poll else self._current_metrics
        if not metrics.gpu_available:
            return False, "GPU/NVML telemetry not available"

        if metrics.vram_free_mb < required_mb:
            return False, (
                f"Insufficient physical VRAM: {metrics.vram_free_mb:.1f} MB free, "
                f"{required_mb:.1f} MB required"
            )

        if metrics.vram_total_mb > 0:
            projected_used = metrics.vram_used_mb + required_mb
            projected_pct = (projected_used / metrics.vram_total_mb) * 100.0
            if projected_pct > self.vram_threshold:
                return False, (
                    f"Projected VRAM utilization ({projected_pct:.1f}%) exceeds threshold "
                    f"({self.vram_threshold:.1f}%)"
                )

        return True, f"VRAM headroom verified ({metrics.vram_free_mb:.1f} MB free)"


class _ActivityContext:
    """Async context manager for ResourceGovernor.activity()."""

    def __init__(
        self,
        governor: ResourceGovernor,
        activity_type: Union[ActivityType, str],
        label: Optional[str] = None
    ):
        self._governor = governor
        self._activity_type = activity_type
        self._label = label
        self._activity_id: Optional[str] = None

    async def __aenter__(self):
        self._activity_id = self._governor.begin_activity(self._activity_type, self._label)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._activity_id:
            self._governor.end_activity(self._activity_id)
        return False
