import asyncio

import pytest
from fastapi.testclient import TestClient

from app.awareness.briefing import address_suffix, build_briefing, format_spoken
from app.awareness.monitor import AwarenessMonitor
from app.awareness.observations import Observation, Severity, SystemSnapshot
from app.awareness.rules import (
    Thresholds,
    battery_level,
    cpu_saturation,
    disk_space,
    gpu_thermal,
    heavy_external_app,
    model_evicted,
    ram_pressure,
    vram_pressure,
)
from app.main import app
from app.persona.profiles import ASSISTANT, JARVIS

THRESHOLDS = Thresholds()


def snapshot(**overrides) -> SystemSnapshot:
    """A snapshot of a healthy machine, with the fields under test overridden."""
    base = dict(
        cpu_percent=10.0,
        ram_percent=40.0,
        ram_used_mb=6000.0,
        ram_total_mb=16000.0,
        gpu_available=True,
        gpu_name="RTX 4060",
        gpu_util_percent=20.0,
        vram_used_mb=2000.0,
        vram_total_mb=8192.0,
        vram_free_mb=6192.0,
        vram_util_percent=24.0,
        gpu_temp_c=55.0,
        disk_free_gb=200.0,
        disk_total_gb=500.0,
        disk_percent=60.0,
        battery_percent=90.0,
        battery_plugged=True,
    )
    base.update(overrides)
    return SystemSnapshot(**base)


# ------------------------------------------------------------------- rules


def test_healthy_machine_trips_no_rules():
    healthy = snapshot()
    for rule in (
        vram_pressure,
        gpu_thermal,
        ram_pressure,
        cpu_saturation,
        disk_space,
        battery_level,
        model_evicted,
        heavy_external_app,
    ):
        assert rule(healthy, THRESHOLDS) is None, rule.__name__


def test_vram_escalates_from_warning_to_critical():
    warning = vram_pressure(snapshot(vram_util_percent=90.0), THRESHOLDS)
    critical = vram_pressure(snapshot(vram_util_percent=99.0), THRESHOLDS)

    assert warning.severity is Severity.WARNING
    assert critical.severity is Severity.CRITICAL
    assert critical.severity.rank > warning.severity.rank


def test_vram_rule_ignores_machines_without_a_gpu():
    assert vram_pressure(snapshot(gpu_available=False, vram_util_percent=99.0), THRESHOLDS) is None


def test_thermal_and_disk_and_cpu_rules_trip_at_thresholds():
    assert gpu_thermal(snapshot(gpu_temp_c=88.0), THRESHOLDS).severity is Severity.CRITICAL
    assert gpu_thermal(snapshot(gpu_temp_c=81.0), THRESHOLDS).severity is Severity.WARNING
    assert gpu_thermal(snapshot(gpu_temp_c=None), THRESHOLDS) is None

    assert disk_space(snapshot(disk_free_gb=3.0), THRESHOLDS).severity is Severity.CRITICAL
    assert disk_space(snapshot(disk_free_gb=15.0), THRESHOLDS).severity is Severity.WARNING

    assert cpu_saturation(snapshot(cpu_percent=95.0), THRESHOLDS).severity is Severity.NOTICE
    assert ram_pressure(snapshot(ram_percent=91.0), THRESHOLDS).severity is Severity.WARNING


def test_battery_rule_is_silent_while_plugged_in():
    assert battery_level(snapshot(battery_percent=5.0, battery_plugged=True), THRESHOLDS) is None
    assert (
        battery_level(snapshot(battery_percent=5.0, battery_plugged=False), THRESHOLDS).severity
        is Severity.CRITICAL
    )


def test_heavy_app_and_eviction_rules_report_context():
    heavy = heavy_external_app(snapshot(heavy_apps=["Unreal Editor"]), THRESHOLDS)
    assert "Unreal Editor" in heavy.title

    assert model_evicted(snapshot(model_unloaded=True), THRESHOLDS).kind == "model_evicted"


# ----------------------------------------------------------------- monitor


@pytest.fixture
def monitor():
    return AwarenessMonitor(governor=None, restate_cooldown_seconds=300.0)


def test_condition_is_announced_once_not_every_poll(monitor):
    hot = snapshot(vram_util_percent=90.0)

    assert len(monitor.evaluate(hot)) == 1
    assert monitor.evaluate(hot) == []
    assert monitor.evaluate(hot) == []


def test_escalation_breaks_through_the_cooldown(monitor):
    monitor.evaluate(snapshot(vram_util_percent=90.0))

    escalated = monitor.evaluate(snapshot(vram_util_percent=99.0))
    assert len(escalated) == 1
    assert escalated[0].severity is Severity.CRITICAL


def test_a_standing_condition_is_restated_after_the_cooldown(monitor):
    monitor.restate_cooldown_seconds = 60.0
    hot = snapshot(vram_util_percent=90.0)

    monitor.evaluate(hot)
    assert monitor.evaluate(snapshot(vram_util_percent=90.0, timestamp=hot.timestamp + 30)) == []

    later = monitor.evaluate(snapshot(vram_util_percent=90.0, timestamp=hot.timestamp + 120))
    assert len(later) == 1


def test_de_escalation_does_not_re_announce(monitor):
    monitor.evaluate(snapshot(vram_util_percent=99.0))

    # Still tripped, but less severe: nothing new to say.
    assert monitor.evaluate(snapshot(vram_util_percent=90.0)) == []


def test_recovery_is_announced_once_when_a_condition_clears(monitor):
    monitor.evaluate(snapshot(vram_util_percent=90.0))

    recovered = monitor.evaluate(snapshot())
    assert len(recovered) == 1
    assert recovered[0].resolved is True
    assert recovered[0].severity is Severity.INFO

    # And not again on the next healthy poll.
    assert monitor.evaluate(snapshot()) == []


def test_independent_conditions_are_tracked_separately(monitor):
    both = monitor.evaluate(snapshot(vram_util_percent=90.0, disk_free_gb=2.0))
    assert {o.kind for o in both} == {"vram_pressure", "disk_space"}

    # Clearing one leaves the other standing and unannounced.
    partial = monitor.evaluate(snapshot(vram_util_percent=90.0))
    assert [o.kind for o in partial] == ["disk_space"]
    assert partial[0].resolved is True
    assert monitor.active_observations() == ["vram_pressure"]


def test_a_failing_rule_does_not_stop_the_others(monitor):
    def broken(_snapshot, _thresholds):
        raise RuntimeError("sensor exploded")

    monitor.rules = (broken, disk_space)
    observations = monitor.evaluate(snapshot(disk_free_gb=2.0))

    assert [o.kind for o in observations] == ["disk_space"]


def test_only_important_observations_interrupt_out_loud(monitor):
    warning = vram_pressure(snapshot(vram_util_percent=90.0), THRESHOLDS)
    notice = cpu_saturation(snapshot(cpu_percent=95.0), THRESHOLDS)
    recovery = Observation(kind="vram_pressure", severity=Severity.INFO, title="ok", resolved=True)

    assert monitor.should_speak(warning) is True
    assert monitor.should_speak(notice) is False
    assert monitor.should_speak(recovery) is False


def test_history_is_queryable_by_cursor(monitor):
    first = monitor.evaluate(snapshot(vram_util_percent=90.0))[0]
    monitor.evaluate(snapshot(vram_util_percent=90.0, disk_free_gb=2.0))

    assert len(monitor.recent()) == 2
    # A cursor at the first observation returns only what came after it.
    assert [o.kind for o in monitor.recent(since_seq=first.seq)] == ["disk_space"]
    assert monitor.recent(since_seq=first.seq + 1) == []


def test_acknowledge_marks_a_specific_observation(monitor):
    observation = monitor.evaluate(snapshot(vram_util_percent=90.0))[0]

    assert monitor.acknowledge(observation.id) is True
    assert monitor.acknowledge("obs_missing") is False
    assert monitor.recent()[0].acknowledged is True


@pytest.mark.asyncio
async def test_subscribers_receive_published_observations(monitor):
    queue = monitor.subscribe()
    monitor.governor = None

    monitor._publish(monitor.evaluate(snapshot(vram_util_percent=99.0)))
    payload = await asyncio.wait_for(queue.get(), timeout=1.0)

    assert payload["kind"] == "vram_pressure"
    assert payload["speak"] is True

    monitor.unsubscribe(queue)
    assert monitor.status()["subscribers"] == 0


@pytest.mark.asyncio
async def test_a_saturated_subscriber_does_not_block_publishing(monitor):
    queue = monitor.subscribe()
    while not queue.full():
        queue.put_nowait({"filler": True})

    # Must not raise despite the queue being full.
    monitor._publish(monitor.evaluate(snapshot(vram_util_percent=99.0)))


@pytest.mark.asyncio
async def test_poll_once_samples_and_evaluates(monitor):
    monitor.collect_snapshot = lambda: snapshot(disk_free_gb=1.0)

    observations = await monitor.poll_once()
    assert [o.kind for o in observations] == ["disk_space"]


@pytest.mark.asyncio
async def test_a_critical_observation_triggers_its_registered_action():
    acted: list[str] = []

    async def evict(observation: Observation):
        acted.append(observation.kind)
        return "evicted"

    monitor = AwarenessMonitor(actions={"vram_pressure": evict})
    monitor.collect_snapshot = lambda: snapshot(vram_util_percent=99.0)

    await monitor.poll_once()
    await asyncio.sleep(0)  # let the fire-and-forget action task run

    assert acted == ["vram_pressure"]
    assert [o.kind for o in monitor.recent()] == ["vram_pressure", "vram_pressure_action"]


@pytest.mark.asyncio
async def test_a_warning_does_not_trigger_the_action_only_critical_does():
    acted: list[str] = []

    async def evict(observation: Observation):
        acted.append(observation.kind)
        return "evicted"

    monitor = AwarenessMonitor(actions={"vram_pressure": evict})
    monitor.collect_snapshot = lambda: snapshot(vram_util_percent=90.0)  # warning, not critical

    await monitor.poll_once()
    await asyncio.sleep(0)

    assert acted == []


@pytest.mark.asyncio
async def test_action_fires_once_per_escalation_not_every_poll():
    calls = 0

    async def evict(observation: Observation):
        nonlocal calls
        calls += 1
        return "evicted"

    monitor = AwarenessMonitor(actions={"vram_pressure": evict})
    hot = snapshot(vram_util_percent=99.0)
    monitor.collect_snapshot = lambda: hot

    await monitor.poll_once()
    await asyncio.sleep(0)
    await monitor.poll_once()  # still critical: must not re-fire
    await asyncio.sleep(0)

    assert calls == 1

    # Recover, then re-trip: the action re-arms.
    monitor.collect_snapshot = lambda: snapshot()
    await monitor.poll_once()
    monitor.collect_snapshot = lambda: hot
    await monitor.poll_once()
    await asyncio.sleep(0)

    assert calls == 2


@pytest.mark.asyncio
async def test_actions_enabled_flag_suppresses_the_action():
    acted: list[str] = []

    async def evict(observation: Observation):
        acted.append(observation.kind)
        return "evicted"

    monitor = AwarenessMonitor(actions={"vram_pressure": evict})
    monitor.actions_enabled = False
    monitor.collect_snapshot = lambda: snapshot(vram_util_percent=99.0)

    await monitor.poll_once()
    await asyncio.sleep(0)

    assert acted == []


@pytest.mark.asyncio
async def test_a_raising_action_does_not_crash_the_poll():
    async def broken(observation: Observation):
        raise RuntimeError("eviction failed")

    monitor = AwarenessMonitor(actions={"vram_pressure": broken})
    monitor.collect_snapshot = lambda: snapshot(vram_util_percent=99.0)

    observations = await monitor.poll_once()
    await asyncio.sleep(0)

    assert [o.kind for o in observations] == ["vram_pressure"]
    # No follow-up "acted" observation, since the action failed.
    assert [o.kind for o in monitor.recent()] == ["vram_pressure"]


def test_collect_snapshot_survives_a_broken_governor():
    class _Broken:
        def collect_metrics(self):
            raise RuntimeError("nvml gone")

        @property
        def status(self):
            raise RuntimeError("nvml gone")

    result = AwarenessMonitor(governor=_Broken()).collect_snapshot()

    # Hardware sampling failed, but psutil-backed fields still populate.
    assert result.disk_total_gb > 0
    assert result.gpu_available is False


# ---------------------------------------------------------------- briefing


def test_address_suffix_follows_the_persona():
    assert address_suffix(JARVIS) == ", sir"
    assert address_suffix(ASSISTANT) == ""


def test_spoken_text_is_rendered_in_the_active_persona_voice():
    observation = vram_pressure(snapshot(vram_util_percent=99.0), THRESHOLDS)

    assert "{address}" in observation.spoken
    assert ", sir" in format_spoken(observation, JARVIS)
    assert "sir" not in format_spoken(observation, ASSISTANT)


def test_briefing_reports_real_numbers_for_both_screen_and_speech():
    payload = build_briefing(
        snapshot(vram_used_mb=4096.0, disk_free_gb=120.0),
        persona=JARVIS,
        active_conditions=["gpu_thermal"],
    )

    assert "**GPU**" in payload["text"]
    assert "4.0 / 8.0 GB" in payload["text"]
    assert "gpu thermal" in payload["text"]
    # Spoken form carries no markdown.
    assert "**" not in payload["spoken"]
    assert "sir" in payload["spoken"]
    assert "gpu thermal" in payload["spoken"]


def test_briefing_says_all_clear_when_nothing_is_wrong():
    payload = build_briefing(snapshot(), persona=JARVIS, active_conditions=[])
    assert "Nothing needs your attention." in payload["spoken"]


def test_briefing_handles_a_machine_with_no_gpu():
    payload = build_briefing(snapshot(gpu_available=False), persona=ASSISTANT)
    assert "not detected" in payload["text"]


# --------------------------------------------------------------------- API


@pytest.fixture
def client(monkeypatch):
    isolated = AwarenessMonitor(governor=None)
    isolated.collect_snapshot = lambda: snapshot(vram_util_percent=99.0)

    import app.routers.awareness as awareness_router

    monkeypatch.setattr(awareness_router, "get_monitor", lambda: isolated)
    return TestClient(app)


def test_status_endpoint_returns_snapshot_and_monitor_state(client):
    body = client.get("/api/awareness/status").json()

    assert body["snapshot"]["gpu_available"] is True
    assert body["monitor"]["enabled"] is True


def test_poll_endpoint_returns_persona_rendered_observations(client):
    body = client.post("/api/awareness/poll").json()

    assert body["observations"]
    observation = body["observations"][0]
    assert observation["kind"] == "vram_pressure"
    assert "{address}" not in observation["spoken"]
    assert observation["speak"] is True


def test_observations_are_listed_and_acknowledged(client):
    client.post("/api/awareness/poll")

    listed = client.get("/api/awareness/observations").json()
    assert listed["active_conditions"] == ["vram_pressure"]

    observation_id = listed["observations"][0]["id"]
    assert client.post(f"/api/awareness/observations/{observation_id}/ack").status_code == 200
    assert client.post("/api/awareness/observations/obs_nope/ack").status_code == 404


def test_briefing_endpoint_speaks_in_the_active_persona(client):
    body = client.get("/api/awareness/briefing").json()

    assert body["text"].startswith("Good")
    assert body["spoken"]
    assert body["snapshot"]["gpu_available"] is True


def test_config_can_be_tuned_and_rejects_bad_severity(client):
    body = client.patch(
        "/api/awareness/config",
        json={"enabled": False, "poll_seconds": 60.0, "min_speak_severity": "critical"},
    ).json()

    assert body["enabled"] is False
    assert body["poll_seconds"] == 60.0
    assert body["min_speak_severity"] == "critical"

    assert (
        client.patch("/api/awareness/config", json={"min_speak_severity": "shouty"}).status_code
        == 400
    )
    assert client.patch("/api/awareness/config", json={"poll_seconds": 0.5}).status_code == 422
