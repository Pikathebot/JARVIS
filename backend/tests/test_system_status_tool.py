"""
Hardware figures reach the model on demand, not on every turn. The prompt used to carry a
SYSTEM STATE line so a persona asking for "concrete numbers" had real ones -- which also put
the machine's vitals into replies that never asked. Now a question about the machine offers
``get_system_status``, which measures fresh, and the per-turn block carries the clock instead.
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock

from app.agent.orchestrator import build_turn_context, current_time_line
from app.agent.permissions import RiskTier, evaluate_tool_permission
from app.agent.tools.registry import TOOL_FUNCTIONS, TOOL_SCHEMAS, get_system_status, tools_for_space
from app.awareness.observations import SystemSnapshot


def test_turn_context_carries_the_clock_not_the_hardware():
    now = datetime(2026, 9, 22, 14, 7, 45, tzinfo=timezone.utc)
    block = build_turn_context("SKILL: x", now=now)
    assert block.startswith("CURRENT TIME: Tuesday 2026-09-22 14:07 UTC")
    assert "SYSTEM STATE" not in block
    assert block.endswith("SKILL: x")
    # Minute resolution: the several requests inside one turn must see an identical prefix.
    assert current_time_line(now) == current_time_line(now.replace(second=3))


def test_the_status_tool_is_offered_in_every_space():
    for mode in ("FREEFORM", "WORKSPACE", "SYSTEM"):
        assert get_system_status in tools_for_space(mode), mode


def test_status_tool_is_registered_low_risk_and_argless():
    assert TOOL_FUNCTIONS["get_system_status"] is get_system_status
    assert TOOL_SCHEMAS["get_system_status"].model_fields == {}
    decision = evaluate_tool_permission("get_system_status", {})
    assert decision.allowed is True and decision.risk_tier == RiskTier.LOW_RISK


def test_status_tool_measures_fresh(monkeypatch):
    import app.main as main_module

    snapshot = SystemSnapshot(
        gpu_available=True, gpu_name="RTX 4060", vram_used_mb=6300, vram_total_mb=8188,
        vram_free_mb=1888, gpu_util_percent=12, gpu_temp_c=53, ram_used_mb=9000,
        ram_total_mb=16000, cpu_percent=7, disk_free_gb=120, disk_total_gb=500,
        governor_status="IDLE",
    )
    monitor = MagicMock()
    monitor.collect_snapshot.return_value = snapshot
    monkeypatch.setattr(main_module, "awareness_monitor", monitor)

    out = get_system_status()

    monitor.collect_snapshot.assert_called_once()
    assert out.startswith("System status (measured just now): ")
    assert "VRAM 6.2/8.0 GB used" in out and "53°C" in out and "CPU 7% load" in out
    assert "governor: IDLE" in out


def test_every_fixed_drive_is_named():
    from collections import namedtuple
    from unittest.mock import patch
    from app.agent.tools import system_status as ss
    Part = namedtuple("Part", "device mountpoint fstype opts")
    Usage = namedtuple("Usage", "total used free percent")
    parts = [Part("C:\\", "C:\\", "NTFS", "rw,fixed"), Part("D:\\", "D:\\", "NTFS", "rw,fixed"),
             Part("E:\\", "E:\\", "", "cdrom")]
    sizes = {"C:\\": Usage(280 * 2**30, 0, 15 * 2**30, 0), "D:\\": Usage(650 * 2**30, 0, 64 * 2**30, 0)}
    with patch.object(ss.psutil, "disk_partitions", return_value=parts), \
         patch.object(ss.psutil, "disk_usage", side_effect=lambda m: sizes[m]):
        assert ss.fixed_drives_line() == "drives: C: 15 of 280 GB free, D: 64 of 650 GB free"
