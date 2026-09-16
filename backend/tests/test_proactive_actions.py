"""
Proactive follow-ups: a heavy app that stays open gets a nudge with numbers, and a full disk
gets told what is taking the space.
"""
import asyncio
from pathlib import Path

import pytest

from app.awareness.actions import describe_disk_offenders, heavy_app_stats, largest_files
from app.awareness.monitor import AwarenessMonitor
from app.awareness.observations import Severity, SystemSnapshot
from app.awareness.rules import Thresholds, heavy_external_app
from app.governor.process_watcher import WatchedProcess

THRESHOLDS = Thresholds()


def snapshot(**overrides) -> SystemSnapshot:
    base = dict(
        gpu_available=True, vram_total_mb=8192.0, vram_used_mb=7000.0, vram_free_mb=1192.0,
        heavy_apps=["Blender"],
    )
    base.update(overrides)
    return SystemSnapshot(**base)


# ------------------------------------------------------------- heavy app

def test_fresh_heavy_app_is_only_a_notice():
    obs = heavy_external_app(snapshot(heavy_app_details={"Blender": {"seconds": 30.0}}), THRESHOLDS)

    assert obs.severity == Severity.NOTICE
    assert "yielding the GPU" in obs.spoken


def test_sustained_heavy_app_escalates_to_a_nudge_with_numbers():
    details = {"Blender": {"seconds": 720.0, "vram_mb": 2150.0, "ram_mb": 3500.0, "cpu_percent": 40.0}}

    obs = heavy_external_app(snapshot(heavy_app_details=details), THRESHOLDS)

    assert obs.severity == Severity.WARNING
    assert "Blender has held 2.1 gigabytes of VRAM and 3.4 gigabytes of RAM and 40 percent of the CPU for 12 minutes" in obs.spoken
    assert "only 1.2 gigabytes free, so my model has little headroom" in obs.spoken
    assert obs.spoken.endswith("Closing it would give that back{address}.")
    assert "{address}" not in obs.detail


def test_nudge_omits_figures_it_could_not_read():
    details = {"Blender": {"seconds": 900.0}}

    obs = heavy_external_app(snapshot(heavy_app_details=details, vram_free_mb=4000.0), THRESHOLDS)

    assert obs.spoken.startswith("Blender has been running for 15 minutes; the GPU has 3.9 gigabytes free.")


def test_nudge_mentions_the_model_being_unloaded():
    details = {"Blender": {"seconds": 900.0, "vram_mb": 3000.0}}

    obs = heavy_external_app(snapshot(heavy_app_details=details, model_unloaded=True), THRESHOLDS)

    assert "my model stays unloaded until it closes" in obs.spoken


def test_longest_running_app_gets_the_nudge_when_several_are_open():
    details = {"Blender": {"seconds": 700.0}, "Unreal Engine": {"seconds": 1500.0}}

    obs = heavy_external_app(
        snapshot(heavy_apps=["Blender", "Unreal Engine"], heavy_app_details=details), THRESHOLDS
    )

    assert obs.title.startswith("Unreal Engine has been running for 25 minutes")


def test_monitor_escalates_once_the_app_has_been_open_long_enough():
    """The escalation NOTICE -> WARNING is what makes the monitor re-announce (and speak) it."""
    monitor = AwarenessMonitor(governor=None, restate_cooldown_seconds=10_000.0)
    monitor.thresholds = Thresholds(heavy_app_nudge_seconds=600.0)

    fresh = snapshot(heavy_app_details=monitor._heavy_app_details(["Blender"], now=0.0))
    first = monitor.evaluate(fresh)
    assert [o.severity for o in first] == [Severity.NOTICE]

    still = snapshot(heavy_app_details=monitor._heavy_app_details(["Blender"], now=300.0))
    assert monitor.evaluate(still) == []  # not yet, and not restated either

    sustained = snapshot(heavy_app_details=monitor._heavy_app_details(["Blender"], now=601.0))
    escalated = monitor.evaluate(sustained)
    assert [o.severity for o in escalated] == [Severity.WARNING]
    assert monitor.should_speak(escalated[0])
    assert "10 minutes" in escalated[0].spoken

    # Closed: the duration is forgotten, so a relaunch starts a fresh clock.
    monitor._heavy_app_details([], now=700.0)
    assert monitor._heavy_app_details(["Blender"], now=800.0)["Blender"]["seconds"] == 0.0


def test_heavy_app_stats_measure_the_matching_processes(monkeypatch):
    import app.awareness.actions as actions

    class Mem:
        rss = 512 * 1024 * 1024

    class Proc:
        def __init__(self, pid, name):
            self.info = {"pid": pid, "name": name, "memory_info": Mem()}

        def cpu_percent(self, interval=None):
            return 12.5

    procs = [Proc(1, "blender.exe"), Proc(2, "blender.exe"), Proc(3, "notepad.exe")]
    monkeypatch.setattr(actions.psutil, "process_iter", lambda attrs=None: procs)
    monkeypatch.setattr(actions, "_gpu_memory_by_pid", lambda cap_mb=None: {1: 1500.0, 2: 600.0, 3: 50.0})

    stats = heavy_app_stats(["Blender"], [WatchedProcess(match="blender.exe", label="Blender")])

    assert stats == {"Blender": {"vram_mb": 2100.0, "ram_mb": 1024.0, "cpu_percent": 25.0}}


def test_bogus_per_process_readings_above_the_card_total_are_dropped(monkeypatch):
    import app.awareness.actions as actions

    monkeypatch.setattr(actions, "_gpu_memory_by_pid_pdh", lambda: {1: 90_812.0, 2: 4_041.0})
    monkeypatch.setattr(actions.os, "name", "nt")

    assert actions._gpu_memory_by_pid(cap_mb=8192.0) == {2: 4_041.0}


def test_heavy_app_stats_are_empty_without_labels_or_rules():
    assert heavy_app_stats([], [WatchedProcess(match="x", label="X")]) == {}
    assert heavy_app_stats(["X"], []) == {}


# ------------------------------------------------------------ disk space

@pytest.fixture
def tree(tmp_path):
    ws = tmp_path / "workspace" / "projects" / "p1" / "artifacts"
    ws.mkdir(parents=True)
    (ws / "big.bin").write_bytes(b"x" * (3 * 1024 * 1024))
    (ws / "small.txt").write_bytes(b"x" * 10)
    models = tmp_path / "models" / "qwen"
    models.mkdir(parents=True)
    (models / "model.gguf").write_bytes(b"x" * (5 * 1024 * 1024))
    return tmp_path


def test_largest_files_are_ranked_across_roots(tree):
    biggest = largest_files([tree / "workspace", tree / "models"], top_n=2)

    assert [p.name for p, _ in biggest] == ["model.gguf", "big.bin"]
    assert [size for _, size in biggest] == [5 * 1024 * 1024, 3 * 1024 * 1024]


def test_disk_offenders_are_described_relative_to_their_root(tree):
    spoken = describe_disk_offenders([tree / "workspace", tree / "models"], top_n=2)

    assert spoken.startswith("The largest things I manage are models/qwen/model.gguf at 5 megabytes, and workspace/projects/p1/artifacts/big.bin at 3 megabytes.")
    assert spoken.endswith("Removing what you no longer need would free the most space.")


def test_disk_offenders_stay_quiet_when_nothing_is_there(tmp_path):
    assert describe_disk_offenders([tmp_path / "missing"]) is None


@pytest.mark.asyncio
async def test_disk_space_critical_triggers_the_offender_action():
    spoken_lines = []

    async def name_offenders(observation):
        spoken_lines.append(observation.kind)
        return "The largest things I manage are x."

    monitor = AwarenessMonitor(governor=None, actions={"disk_space": name_offenders})
    monitor.collect_snapshot = lambda: snapshot(
        heavy_apps=[], disk_total_gb=500.0, disk_free_gb=2.0, disk_percent=99.0
    )

    await monitor.poll_once()
    await asyncio.sleep(0)

    assert spoken_lines == ["disk_space"]
    kinds = [o.kind for o in monitor.recent()]
    assert "disk_space_action" in kinds
