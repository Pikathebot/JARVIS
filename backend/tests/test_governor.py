import asyncio
import time
import pytest
import httpx
from unittest.mock import AsyncMock, patch
from app.main import app, governor
from app.governor.resource_governor import (
    ResourceGovernor,
    SystemMetrics,
    ActivityType,
    GovernorStatus,
    GovernorEvent,
)


def test_governor_telemetry_collection():
    """Verify live hardware telemetry collection."""
    gov = ResourceGovernor(enabled=True)
    metrics = gov.collect_metrics()
    
    assert metrics.cpu_percent >= 0.0
    assert metrics.ram_percent > 0.0
    assert metrics.ram_total_mb > 0.0
    assert metrics.ram_used_mb > 0.0
    assert isinstance(metrics.gpu_available, bool)
    assert isinstance(metrics.raw_throttled, bool)
    assert isinstance(metrics.throttled, bool)


def test_activity_registry_concurrent():
    """Verify concurrent activities keep governor busy until all conclude."""
    gov = ResourceGovernor(enabled=True)
    assert gov.is_busy is False
    assert gov.status == GovernorStatus.NORMAL

    # Begin scheduler job
    act1 = gov.begin_activity(ActivityType.SCHEDULER_JOB, label="backup")
    assert gov.is_busy is True
    assert gov.status == GovernorStatus.BUSY
    assert len(gov.active_activities) == 1

    # Begin concurrent inference
    act2 = gov.begin_activity(ActivityType.INFERENCING, label="chat_turn")
    assert gov.is_busy is True
    assert gov.is_inferencing is True
    assert len(gov.active_activities) == 2

    # End scheduler job -> inference still active
    gov.end_activity(act1)
    assert gov.is_busy is True
    assert gov.is_inferencing is True
    assert len(gov.active_activities) == 1

    # End inference
    gov.end_activity(act2)
    assert gov.is_busy is False
    assert gov.status == GovernorStatus.NORMAL
    assert len(gov.active_activities) == 0


@pytest.mark.anyio
async def test_activity_context_manager():
    """Verify async context manager syntax for activity registry."""
    gov = ResourceGovernor(enabled=True)
    assert gov.is_busy is False

    async with gov.activity(ActivityType.RAG_INDEXING, label="knowledge_base"):
        assert gov.is_busy is True
        assert gov.status == GovernorStatus.BUSY
        assert "rag_indexing" in gov.active_activity_types

    assert gov.is_busy is False
    assert gov.status == GovernorStatus.NORMAL


def test_backward_compatibility_wrappers():
    """Verify legacy set_inferencing and set_loading_model work as expected."""
    gov = ResourceGovernor(enabled=True)
    
    gov.set_loading_model(True)
    assert gov.is_busy is True
    assert gov.is_loading_model is True
    assert gov.status == GovernorStatus.LOADING

    gov.set_loading_model(False)
    assert gov.is_busy is False

    gov.set_inferencing(True)
    assert gov.is_busy is True
    assert gov.is_inferencing is True

    gov.set_inferencing(False)
    assert gov.is_busy is False


def test_debounce_hysteresis():
    """Verify single-poll spike does not throttle, sustained breaches do, and recovery requires consecutive polls."""
    gov = ResourceGovernor(
        enabled=True,
        cpu_threshold=0.01,
        gpu_threshold=0.01,
        ram_threshold=0.01,
        sustained_breach_polls=3,
        recovery_polls=2,
        startup_grace_seconds=0.0
    )

    # Poll 1 (breach 1/3)
    m1 = gov.collect_metrics()
    assert m1.raw_throttled is True
    assert m1.throttled is False  # Debounce not yet satisfied

    # Poll 2 (breach 2/3)
    m2 = gov.collect_metrics()
    assert m2.raw_throttled is True
    assert m2.throttled is False

    # Poll 3 (breach 3/3 -> transitions to throttled)
    m3 = gov.collect_metrics()
    assert m3.raw_throttled is True
    assert m3.throttled is True
    assert gov.status == GovernorStatus.THROTTLED

    # Now restore healthy thresholds (using high values so live CPU/GPU load doesn't trigger)
    gov.cpu_threshold = 999.0
    gov.gpu_threshold = 999.0
    gov.ram_threshold = 999.0
    gov.vram_threshold = 999.0

    # Recovery Poll 1 (1/2) -> still throttled
    m4 = gov.collect_metrics()
    assert m4.raw_throttled is False
    assert m4.throttled is True

    # Recovery Poll 2 (2/2) -> cleared back to healthy
    m5 = gov.collect_metrics()
    assert m5.raw_throttled is False
    assert m5.throttled is False
    assert gov.status == GovernorStatus.NORMAL


def test_status_hierarchy_and_manual_overrides():
    """Verify 6-tier status hierarchy and manual pause/resume/override."""
    gov = ResourceGovernor(enabled=True)

    # 1. Default -> IDLE
    assert gov.status == GovernorStatus.IDLE
    assert gov.status == "IDLE"

    # 2. Manual Pause beats everything -> PAUSED
    gov.force_pause(reason="user requested pause")
    assert gov.status == GovernorStatus.PAUSED
    assert gov.manual_override_active is True
    assert gov.is_manual_override is False  # is_manual_override is True only for timed override

    # 3. Resume override clears pause and overrides load -> IDLE
    gov.force_resume_ignore_metrics(duration_seconds=60)
    assert gov.status == GovernorStatus.IDLE
    assert gov.is_manual_override is True
    assert gov.manual_override_active is True

    # 4. External app reporting
    gov.force_resume()  # back to auto
    assert gov.status == GovernorStatus.IDLE

    gov.report_external_app("Blender", present=True)
    assert gov.status == GovernorStatus.PAUSED

    # Manual pause still overrides
    gov.force_pause("emergency")
    assert gov.status == GovernorStatus.PAUSED

    # Clear manual pause -> still paused because of external app
    gov.force_resume()
    assert gov.status == GovernorStatus.PAUSED

    # Clear external app -> returns to IDLE
    gov.report_external_app("Blender", present=False)
    assert gov.status == GovernorStatus.IDLE


def test_6_tier_status_and_derived_fields():
    """Verify all 6 tiers (IDLE, RUNNING, PAUSED, LOADING, UNLOADED, ERROR) and derived fields."""
    gov = ResourceGovernor(enabled=True)

    # 1. IDLE
    assert gov.status == GovernorStatus.IDLE
    assert gov.model_unloaded is False
    assert gov.manual_override_active is False
    assert gov.pending_reload is False

    # 2. LOADING
    act_load = gov.begin_activity(ActivityType.MODEL_LOADING, label="qwen3.8-9b-distill")
    assert gov.status == GovernorStatus.LOADING
    gov.end_activity(act_load)

    # 3. RUNNING
    act_run = gov.begin_activity(ActivityType.INFERENCING, label="chat")
    assert gov.status == GovernorStatus.RUNNING
    gov.end_activity(act_run)

    # 4. PAUSED (via manual pause or external app)
    gov.force_pause("user maintenance")
    assert gov.status == GovernorStatus.PAUSED
    assert gov.manual_override_active is True
    gov.force_resume()

    # 5. UNLOADED (via model_unloaded / throttle)
    gov._add_unload_reason("metrics_throttle", "metrics", "metrics_throttle")
    assert gov.model_unloaded is True
    assert gov.status == GovernorStatus.UNLOADED
    gov._remove_unload_reason("metrics_throttle")
    assert gov.model_unloaded is False

    # 6. ERROR
    gov._error_state = True
    assert gov.status == GovernorStatus.ERROR
    gov._error_state = False
    assert gov.status == GovernorStatus.IDLE


def test_history_raw_reasons_auditability():
    """
    Check 1 & 2 (and Bug 4):
    - raw_reasons on a PAUSED transition caused by report_external_app() contains exactly
      ['external app detected: {label}'], even if snap.throttle_reasons is empty.
    - When metrics breach triggers the same transition, raw_reasons equals list(snap.throttle_reasons).
    - Recovery transitions (app close, manual resume, activity end) preserve audit context in raw_reasons.
    """
    gov = ResourceGovernor(enabled=True)

    # 1. External App Launch & Close Transitions (Bug 4)
    gov.report_external_app("Cyberpunk 2077", present=True)
    assert gov.status == GovernorStatus.PAUSED
    assert gov.get_history(limit=1)[0].raw_reasons == ["external app detected: Cyberpunk 2077"]

    gov.report_external_app("Cyberpunk 2077", present=False)
    assert gov.status == GovernorStatus.IDLE
    assert gov.get_history(limit=1)[0].raw_reasons == ["external app closed: Cyberpunk 2077"]

    # 2. Manual Pause & Resume Transitions (Bug 4)
    gov.force_pause("routine maintenance")
    assert gov.status == GovernorStatus.PAUSED
    assert gov.get_history(limit=1)[0].raw_reasons == ["manual pause: routine maintenance"]

    gov.force_resume()
    assert gov.status == GovernorStatus.IDLE
    assert gov.get_history(limit=1)[0].raw_reasons == ["manual resume: restored automated governance"]

    # 3. Activity Begin & End Transitions (Bug 4)
    act_id = gov.begin_activity(ActivityType.INFERENCING, label="chat_turn")
    assert gov.status == GovernorStatus.RUNNING
    assert gov.get_history(limit=1)[0].raw_reasons == ["activity begun: inferencing (chat_turn)"]

    gov.end_activity(act_id)
    assert gov.status == GovernorStatus.IDLE
    assert gov.get_history(limit=1)[0].raw_reasons == ["activity ended: inferencing (chat_turn)"]

    # 4. Metrics Breach Transition (Check 2)
    fake_metrics = SystemMetrics(
        cpu_percent=99.0,
        raw_throttled=True,
        throttled=True,
        throttle_reasons=["CPU utilization (99.0%) exceeds threshold (92.0%)"]
    )
    gov._debounced_throttled = True
    gov._debounced_reasons = list(fake_metrics.throttle_reasons)
    gov._check_status_transition(fake_metrics)

    latest_event = gov.get_history(limit=1)[0]
    assert latest_event.to_status == "UNLOADED"
    assert latest_event.raw_reasons == list(fake_metrics.throttle_reasons)


def test_multi_external_app_isolation():
    """
    Bug 6 Fix:
    - Multiple active external apps maintain independent reason tracking.
    - Closing one app while another is running preserves PAUSED state and avoids premature reload.
    """
    reloaded = []

    def mock_reload():
        reloaded.append(True)

    gov = ResourceGovernor(enabled=True, on_reload=mock_reload)

    # Launch App 1
    gov.report_external_app("Blender", present=True)
    assert gov.status == GovernorStatus.PAUSED
    assert "external_app:Blender" in gov._unload_reasons

    # Launch App 2
    gov.report_external_app("Unreal Engine", present=True)
    assert gov.status == GovernorStatus.PAUSED
    assert "external_app:Unreal Engine" in gov._unload_reasons
    assert len(gov._external_apps_active) == 2

    # Close App 1 -> App 2 is still running!
    gov.report_external_app("Blender", present=False)
    assert "external_app:Blender" not in gov._unload_reasons
    assert "external_app:Unreal Engine" in gov._unload_reasons
    assert gov.status == GovernorStatus.PAUSED
    assert len(reloaded) == 0  # No premature reload!

    # Close App 2 -> all apps closed -> now reload triggers and state becomes IDLE
    gov.report_external_app("Unreal Engine", present=False)
    assert gov.status == GovernorStatus.IDLE


def test_expired_unload_reasons_triggers_transition_and_reload():
    """
    Bug 7 Fix:
    - Expired unload reasons are evicted, transition history is recorded, and reload is scheduled.
    """
    reloaded = []

    def mock_reload():
        reloaded.append(True)

    gov = ResourceGovernor(enabled=True, on_reload=mock_reload)

    # Add a timed unload reason that expires in 0.05s and trigger initial status check
    gov._add_unload_reason("timed_test_reason", "metrics", "test", duration_seconds=0.05)
    gov._check_status_transition()
    assert gov.model_unloaded is True
    assert gov.status == GovernorStatus.UNLOADED

    # Sleep past expiration
    time.sleep(0.1)

    # Clean up expired reasons
    expired = gov._cleanup_expired_unload_reasons()
    assert len(expired) == 1
    assert gov.model_unloaded is False
    assert gov.status == GovernorStatus.IDLE

    # History captures the expiration reason
    history = gov.get_history(limit=1)
    assert len(history) >= 1
    assert history[0].to_status == "IDLE"
    assert "unload reason expired: timed_test_reason" in history[0].raw_reasons[0]


@pytest.mark.anyio
async def test_reload_retry_backoff_and_permanent_error_state():
    """
    Bugs 5 & 8 Fix:
    - Transient reload failures retry and succeed.
    - Permanent reload failures exhaust retries, clear _pending_reload, and transition to ERROR state.
    """
    # 1. Transient failure recovering on 2nd attempt
    attempts = 0

    async def flaky_reload():
        nonlocal attempts
        attempts += 1
        if attempts < 2:
            return False  # transient failure
        return True  # success

    gov_flaky = ResourceGovernor(enabled=True, on_reload=flaky_reload)
    gov_flaky._pending_reload = True
    gov_flaky._trigger_reload_callback(vram_settle_delay_seconds=0.01)

    # Wait for retry loop to complete
    await asyncio.sleep(0.8)
    assert attempts == 2
    assert gov_flaky.pending_reload is False
    assert gov_flaky.status == GovernorStatus.IDLE

    # 2. Permanent failure exhausting retries (Bug 8)
    async def always_failing_reload():
        raise RuntimeError("CUDA OOM: permanent hardware error")

    gov_fail = ResourceGovernor(enabled=True, on_reload=always_failing_reload)
    gov_fail._pending_reload = True
    gov_fail._trigger_reload_callback(vram_settle_delay_seconds=0.01)

    # Wait for all 3 retries with backoff to exhaust
    await asyncio.sleep(1.8)
    # Must clear pending_reload to avoid infinite hanging
    assert gov_fail.pending_reload is False
    # Must transition to ERROR state
    assert gov_fail.status == GovernorStatus.ERROR
    history = gov_fail.get_history(limit=1)
    assert history[0].to_status == "ERROR"
    assert "model reload failed after maximum retries" in history[0].raw_reasons[0]


@pytest.mark.anyio
async def test_unload_reason_tracker_expiry_and_disjoint_state():
    """
    Check 3 & 8:
    - Metrics poll loop recovery branch does not reset unload state when external app is active.
    - Reason tracker expiry operates on disjoint state.
    """
    reloaded_called = []

    def mock_reload():
        reloaded_called.append(True)

    gov = ResourceGovernor(
        enabled=True,
        poll_interval=0.05,
        on_reload=mock_reload
    )

    # 1. External app launches and unloads model
    gov.report_external_app("DaVinci Resolve", present=True)
    gov._add_unload_reason("external_app:DaVinci Resolve", "external_app", "DaVinci Resolve")
    assert gov.model_unloaded is True
    assert gov.status == GovernorStatus.PAUSED

    # 2. Metrics poll loop runs with healthy metrics -> must NOT reset unload state!
    fake_healthy_metrics = SystemMetrics(cpu_percent=5.0, gpu_util_percent=10.0, raw_throttled=False, throttled=False)
    gov.collect_metrics = lambda: fake_healthy_metrics

    await gov.start()
    try:
        await asyncio.sleep(0.15)
        # Verify model is STILL unloaded despite healthy metrics
        assert gov.model_unloaded is True
        assert gov.status == GovernorStatus.PAUSED
        assert "external_app:DaVinci Resolve" in gov._unload_reasons

        # 3. External app closes -> unload reason cleared and reload triggered
        gov.report_external_app("DaVinci Resolve", present=False)
        await asyncio.sleep(0.6)
        assert gov.model_unloaded is False
        assert gov.status == GovernorStatus.IDLE
        assert len(reloaded_called) >= 1
    finally:
        await gov.stop()


@pytest.mark.anyio
async def test_timed_override_expiry_and_automatic_reload():
    """
    Check 5, 6, 7:
    - is_manual_override returns True only during active timed override.
    - Timed override expires after duration_seconds and causes automatic reload and reversion.
    """
    reloaded = []

    def mock_reload():
        reloaded.append(True)

    gov = ResourceGovernor(
        enabled=True,
        poll_interval=0.05,
        on_reload=mock_reload
    )

    # Set governor throttled/paused initially
    gov.report_external_app("Blender", present=True)
    assert gov.status == GovernorStatus.PAUSED
    assert gov.is_manual_override is False

    # Force resume with 0.1s timed override
    gov.force_resume_ignore_metrics(duration_seconds=0.1)
    assert gov.is_manual_override is True
    assert gov.manual_override_active is True
    assert gov.status == GovernorStatus.IDLE

    # Wait for override to expire
    await asyncio.sleep(0.2)

    # Live check expiration
    assert gov.is_manual_override is False
    assert gov.status == GovernorStatus.PAUSED
    await asyncio.sleep(0.05)
    assert len(reloaded) >= 1


def test_history_ring_buffer():
    """Verify status transitions are recorded with snapshots in history ring buffer."""
    gov = ResourceGovernor(enabled=True)
    
    gov.force_pause("maintenance")
    gov.force_resume()
    gov.begin_activity(ActivityType.MODEL_LOADING, label="qwen3.8-9b-distill")

    history = gov.get_history(limit=10)
    assert len(history) >= 3
    assert history[0].to_status == "LOADING"
    assert history[1].to_status == "IDLE"
    assert history[2].to_status == "PAUSED"


@pytest.mark.anyio
async def test_governor_auto_unload_callback():
    """Verify governor triggers auto-unload callback on sustained debounced throttle."""
    unloaded_called = []

    async def mock_unload():
        unloaded_called.append(True)

    gov = ResourceGovernor(
        enabled=True,
        gpu_threshold=80.0,
        poll_interval=0.05,
        sustained_breach_polls=1,
        auto_unload_on_throttle=True,
        on_throttle_unload=mock_unload,
        startup_grace_seconds=0.0
    )

    fake_metrics = SystemMetrics(
        cpu_percent=10.0,
        ram_percent=50.0,
        gpu_available=True,
        gpu_util_percent=95.0,
        raw_throttled=True,
        throttled=True,
        throttle_reasons=["GPU compute utilization (95.0%) exceeds threshold (80.0%)"]
    )

    def fake_collect():
        gov._debounced_throttled = True
        gov._debounced_reasons = ["GPU compute utilization (95.0%) exceeds threshold (80.0%)"]
        return fake_metrics

    gov.collect_metrics = fake_collect

    await gov.start()
    try:
        await asyncio.sleep(0.35)
        assert len(unloaded_called) >= 1
        assert gov.model_unloaded is True
    finally:
        await gov.stop()


# --- API Endpoints Tests ---

@pytest.mark.anyio
async def test_api_governor_status_and_overrides():
    """Verify /governor/status, /governor/pause, /governor/resume, and /governor/history endpoints."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ac:
        # 1. GET /governor/status
        res = await ac.get("/governor/status")
        assert res.status_code == 200
        data = res.json()
        assert "status" in data
        assert "is_manual_override" in data
        assert "manual_override_active" in data
        assert "pending_reload" in data
        assert "active_activities" in data
        assert "metrics" in data

        # 2. POST /governor/pause
        pause_res = await ac.post("/governor/pause", json={"reason": "test pause"})
        assert pause_res.status_code == 200
        assert pause_res.json()["governor_status"] == "PAUSED"

        # Verify status endpoint reflects pause
        status_paused = (await ac.get("/governor/status")).json()
        assert status_paused["status"] == "PAUSED"
        assert status_paused["manual_override_active"] is True

        # 3. POST /governor/resume-override
        override_res = await ac.post("/governor/resume-override", json={"duration_seconds": 30.0})
        assert override_res.status_code == 200
        assert override_res.json()["governor_status"] == "IDLE"

        # Verify status endpoint returns override_expires_at
        status_override = (await ac.get("/governor/status")).json()
        assert status_override["is_manual_override"] is True
        assert status_override["override_expires_at"] is not None

        # 4. POST /governor/resume
        resume_res = await ac.post("/governor/resume")
        assert resume_res.status_code == 200
        assert resume_res.json()["governor_status"] == "IDLE"

        # 5. POST /governor/clear-error
        governor._error_state = True
        governor._error_reason = "simulated error"
        assert governor.status == GovernorStatus.ERROR
        clear_res = await ac.post("/governor/clear-error")
        assert clear_res.status_code == 200
        assert clear_res.json()["governor_status"] == "IDLE"
        assert governor.status == GovernorStatus.IDLE

        # 6. POST /governor/force-reload
        # Guard test: when healthy and not unloaded -> returns noop
        reload_noop = await ac.post("/governor/force-reload")
        assert reload_noop.status_code == 200
        assert reload_noop.json()["status"] == "noop"
        assert reload_noop.json()["initiated"] is False

        # When pending_reload or unloaded -> returns ok and initiated True
        governor._pending_reload = True
        reload_res = await ac.post("/governor/force-reload")
        assert reload_res.status_code == 200
        assert reload_res.json()["status"] == "ok"
        assert reload_res.json()["initiated"] is True

        # 7. GET /governor/history
        hist_res = await ac.get("/governor/history?limit=10")
        assert hist_res.status_code == 200
        events = hist_res.json()
        assert isinstance(events, list)
        assert len(events) >= 2


@pytest.mark.anyio
async def test_api_models_load_wrapped_in_governor_activity():
    """Verify /models/load wraps execution in governor.activity(ActivityType.MODEL_LOADING)."""
    observed_statuses = []

    class FakePM:
        async def ensure_running(self, model_name: str = "main"):
            # Capture governor status during the active load call
            observed_statuses.append(governor.status)
            return True

    with patch("app.main.get_runtime_process_manager", return_value=FakePM()):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ac:
            res = await ac.post("/models/load", json={"model_name": "qwen3.5-9b", "backend": "llama_cpp"})
            assert res.status_code == 200
            assert res.json()["success"] is True
            assert res.json()["model"] == "qwen3.5-9b"

    assert len(observed_statuses) == 1
    assert observed_statuses[0] == GovernorStatus.LOADING
    assert governor.status == GovernorStatus.IDLE


def test_override_expires_at_and_manual_recovery_methods():
    """Verify override_expires_at, clear_error(), and force_reload() methods."""
    reloads = []

    def mock_reload():
        reloads.append(True)

    gov = ResourceGovernor(enabled=True, on_reload=mock_reload)

    # 1. override_expires_at
    assert gov.override_expires_at is None
    gov.force_resume_ignore_metrics(duration_seconds=60.0)
    assert gov.override_expires_at is not None
    assert gov.override_expires_at > time.time()
    gov.force_resume()
    assert gov.override_expires_at is None

    # 2. clear_error()
    gov._error_state = True
    gov._error_reason = "fatal test failure"
    gov._check_status_transition()
    assert gov.status == GovernorStatus.ERROR
    gov.clear_error()
    assert gov.status == GovernorStatus.IDLE
    history = gov.get_history(limit=1)
    assert history[0].to_status == "IDLE"
    assert "error cleared manually" in history[0].raw_reasons[0]

    # 3. force_reload() with state guard
    assert len(reloads) == 0
    # Guard: IDLE and not unloaded -> early return False (noop)
    res_noop = gov.force_reload()
    assert res_noop is False
    assert len(reloads) == 0
    assert gov.pending_reload is False

    # When pending_reload or model_unloaded is True -> executes reload
    gov._pending_reload = True
    res_ok = gov.force_reload()
    assert res_ok is True
    assert len(reloads) == 1
    assert gov.pending_reload is False


@pytest.mark.anyio
async def test_retry_counter_local_isolation_across_invocations():
    """Verify that every _trigger_reload_callback gets a fresh 3-attempt retry loop."""
    attempts = []

    async def fail_then_succeed_on_new_invocation():
        attempts.append(time.time())
        # First 3 attempts fail
        if len(attempts) <= 3:
            raise RuntimeError("Transient CUDA memory lock")
        return True

    gov = ResourceGovernor(enabled=True, on_reload=fail_then_succeed_on_new_invocation)

    # 1. First invocation: fails all 3 attempts -> enters ERROR
    gov._pending_reload = True
    gov._trigger_reload_callback(vram_settle_delay_seconds=0.01)
    await asyncio.sleep(4.0)

    assert len(attempts) == 3
    assert gov.status == GovernorStatus.ERROR
    assert gov.pending_reload is False

    # 2. Clear error & invoke force_reload: gets a brand new 3-attempt cycle
    gov.clear_error()
    assert gov.status == GovernorStatus.IDLE

    # Force reload triggers fresh cycle: attempt 4 succeeds!
    gov._pending_reload = True
    gov.force_reload()
    await asyncio.sleep(0.3)

    assert len(attempts) == 4
    assert gov.status == GovernorStatus.IDLE
    assert gov.pending_reload is False





# --- VRAM attribution: the model's own footprint is never a reason to evict the model ---

def _metrics(total=8188.0, used=7900.0, model=5987.0, gpu_util=45.0):
    from app.governor.resource_governor import SystemMetrics
    m = SystemMetrics(gpu_available=True, vram_total_mb=total, vram_used_mb=used,
                      vram_util_percent=round(used / total * 100.0, 1), gpu_util_percent=gpu_util)
    m.model_vram_mb = model
    m.external_vram_mb = max(0.0, used - model)
    return m


def test_vram_rule_ignores_the_models_own_footprint():
    gov = ResourceGovernor(enabled=True, vram_threshold=92.0, external_vram_floor_mb=2048.0, startup_grace_seconds=0.0)
    # The steady state that used to evict mid-conversation: 96.5% with a busy desktop.
    assert gov._vram_reasons(_metrics(used=7900.0, model=5987.0, gpu_util=45.0)) == []
    # A real external workload: 2.5 GB beyond the model.
    reasons = gov._vram_reasons(_metrics(used=7900.0, model=5300.0))
    assert reasons and "External VRAM use" in reasons[0]
    # The hardware ceiling is always a reason -- the model itself is about to page.
    reasons = gov._vram_reasons(_metrics(used=8150.0, model=5987.0))
    assert reasons and "hardware ceiling" in reasons[0]
    # Below the threshold nothing fires, whatever the split.
    assert gov._vram_reasons(_metrics(used=7000.0, model=1000.0)) == []
    # No model of ours loaded: everything is external, the old GPU-busy coupling applies.
    assert gov._vram_reasons(_metrics(used=7900.0, model=0.0, gpu_util=45.0))
    assert gov._vram_reasons(_metrics(used=7900.0, model=0.0, gpu_util=5.0)) == []


def test_collect_metrics_derives_external_share_from_provider():
    gov = ResourceGovernor(enabled=True, model_vram_mb_provider=lambda: 6000.0, startup_grace_seconds=0.0)
    m = gov.collect_metrics()
    if not m.gpu_available:
        pytest.skip("no NVML on this machine")
    assert m.model_vram_mb == 6000.0
    assert m.external_vram_mb == pytest.approx(max(0.0, m.vram_used_mb - 6000.0), abs=0.2)


# --- The floor is relative to what the desktop held at launch ---------------------------

def test_vram_rule_measures_growth_against_the_launch_baseline():
    """The user's ordinary desktop (Steam, Discord, Brave) holds 2.3 GB -- above the absolute
    floor -- and used to be read as a game on every poll. Only growth past the baseline counts."""
    gov = ResourceGovernor(enabled=True, vram_threshold=92.0, external_vram_floor_mb=2048.0,
                           external_vram_growth_mb=1024.0, startup_grace_seconds=0.0)

    def with_baseline(m, baseline):
        m.external_vram_baseline_mb = baseline
        return m

    # 2321 external at launch, still 2321 now, card at 93%: steady state, nothing to yield to.
    assert gov._vram_reasons(with_baseline(_metrics(used=7650.0, model=5329.0), 2321.0)) == []
    # Grew by 700 MiB: under the limit.
    assert gov._vram_reasons(with_baseline(_metrics(used=7650.0, model=4629.0), 2321.0)) == []
    # Grew by 1.1 GB: a game arrived.
    reasons = gov._vram_reasons(with_baseline(_metrics(used=7650.0, model=4229.0), 2321.0))
    assert reasons and "grew" in reasons[0] and "2321" in reasons[0]
    # No baseline for this launch (adopted server): the absolute floor is all there is.
    reasons = gov._vram_reasons(_metrics(used=7650.0, model=5329.0))
    assert reasons and "exceeds floor" in reasons[0]
    # The hardware ceiling still wins regardless of growth.
    reasons = gov._vram_reasons(with_baseline(_metrics(used=8150.0, model=5829.0), 2321.0))
    assert reasons and "hardware ceiling" in reasons[0]


def test_external_baseline_follows_the_desktop_down_and_resets_per_launch():
    """Close Steam after launch, start a game later: the game is measured against the quieter
    desktop, not the launch-time one. A new launch (new reported baseline) starts over."""
    reported = {"value": 2300.0}
    gov = ResourceGovernor(enabled=True, external_vram_growth_mb=1024.0, startup_grace_seconds=0.0,
                           external_vram_baseline_provider=lambda: reported["value"])

    assert gov._track_external_baseline(_metrics(used=7700.0, model=5400.0)) == 2300.0
    # Desktop quietens to 1200: the baseline tracks it.
    assert gov._track_external_baseline(_metrics(used=6600.0, model=5400.0)) == 1200.0
    # Back to 2300 (card at 94%) is now +1100 of growth -- a reason, where the launch-time
    # baseline would hide it.
    m = _metrics(used=7700.0, model=5400.0)
    m.external_vram_baseline_mb = gov._track_external_baseline(m)
    assert m.external_vram_baseline_mb == 1200.0
    assert gov._vram_reasons(m) and "grew 1100" in gov._vram_reasons(m)[0]
    # A relaunch reports a fresh baseline: tracking restarts from it.
    reported["value"] = 2600.0
    assert gov._track_external_baseline(_metrics(used=8000.0, model=5400.0)) == 2600.0
    # No model of ours loaded: no baseline at all.
    assert gov._track_external_baseline(_metrics(used=2600.0, model=0.0)) is None
    reported["value"] = None
    assert gov._track_external_baseline(_metrics(used=8000.0, model=5400.0)) is None


def test_baseline_does_not_track_down_while_the_model_is_paged_out():
    """Seen live: a second process asked for VRAM it could not have, WDDM demoted 3 GB of the
    9B to shared memory, and the card reported 4188 MiB in use against a 5163 MiB model. The
    external share reads 0 then; the baseline must not follow it."""
    gov = ResourceGovernor(enabled=True, external_vram_growth_mb=1024.0, startup_grace_seconds=0.0,
                           external_vram_baseline_provider=lambda: 2042.0)
    resident = _metrics(used=7208.0, model=5163.0)
    resident.model_resident = True
    assert gov._track_external_baseline(resident) == 2042.0

    paged = _metrics(used=4188.0, model=5163.0)
    paged.model_resident = False
    assert paged.external_vram_mb == 0.0
    assert gov._track_external_baseline(paged) == 2042.0   # unchanged, not 0

    # The desktop coming back to 2 GB is the steady state again, not growth.
    back = _metrics(used=7700.0, model=5163.0)
    back.model_resident = True
    back.external_vram_baseline_mb = gov._track_external_baseline(back)
    assert back.external_vram_baseline_mb == 2042.0
    assert gov._vram_reasons(back) == []


def test_vram_headroom_is_free_minus_reserve_or_unknown(monkeypatch):
    from app.governor.resource_governor import SystemMetrics

    gov = ResourceGovernor(enabled=True, startup_grace_seconds=0.0)
    ok = SystemMetrics(gpu_available=True, vram_total_mb=8188.0, vram_used_mb=2321.0, vram_free_mb=5867.0)
    monkeypatch.setattr(gov, "collect_metrics", lambda: ok)
    assert gov.vram_headroom_mb() == 5867.0
    assert gov.vram_headroom_mb(reserve_mb=384.0) == 5483.0
    tiny = SystemMetrics(gpu_available=True, vram_total_mb=8188.0, vram_used_mb=8000.0, vram_free_mb=188.0)
    monkeypatch.setattr(gov, "collect_metrics", lambda: tiny)
    assert gov.vram_headroom_mb(reserve_mb=384.0) == 0.0
    monkeypatch.setattr(gov, "collect_metrics", lambda: SystemMetrics(gpu_available=False))
    assert gov.vram_headroom_mb() is None


# --- Compute from our own llama-server is not an external workload ------------------------

def _gpu_only_governor(**kw) -> ResourceGovernor:
    """GPU compute is the only rule that can fire (a 0% reading breaches a 0.0 threshold, so
    an idle card still counts); every other threshold is out of reach."""
    return ResourceGovernor(
        enabled=True, gpu_threshold=0.0, vram_threshold=999.0, cpu_threshold=999.0,
        ram_threshold=999.0, sustained_breach_polls=1, startup_grace_seconds=0.0, **kw,
    )


def test_gpu_compute_is_not_external_load_while_llama_server_is_processing():
    if not ResourceGovernor(enabled=False).collect_metrics().gpu_available:
        pytest.skip("no NVML on this machine")

    gov = _gpu_only_governor()
    gov._runtime_busy = True
    busy = gov.collect_metrics()
    assert busy.raw_throttled is False

    gov._runtime_busy = False
    idle = gov.collect_metrics()
    assert idle.raw_throttled is True
    assert any("GPU compute" in r for r in idle.throttle_reasons)


@pytest.mark.asyncio
async def test_runtime_busy_probe_treats_unknown_as_idle():
    async def unknown():
        return None

    async def busy():
        return True

    async def broken():
        raise RuntimeError("slots endpoint unreachable")

    assert await _gpu_only_governor()._probe_runtime_busy() is False
    assert await _gpu_only_governor(runtime_busy_provider=unknown)._probe_runtime_busy() is False
    assert await _gpu_only_governor(runtime_busy_provider=busy)._probe_runtime_busy() is True
    assert await _gpu_only_governor(runtime_busy_provider=broken)._probe_runtime_busy() is False
