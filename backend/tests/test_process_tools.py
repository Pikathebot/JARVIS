"""Process tools after the 2026-09-30 tool review: real CPU %, close like clicking X unless the
user says force, every Jarvis process protected, and launch_app's Temp check that never matched."""
from unittest.mock import MagicMock, patch

import app.agent.tools.process_control as pc
from app.agent.permissions import RiskTier, evaluate_launch_app_risk


def _proc(pid, name, children=()):
    m = MagicMock()
    m.pid, m.info = pid, {"pid": pid, "name": name}
    m.name.return_value = name
    m.children.return_value = list(children)
    m.parents.return_value = []
    return m


def test_jarvis_process_ids_cover_sidecars_but_not_launched_apps():
    llama, sidecar, discord = _proc(11, "llama-server.exe"), _proc(12, "llama-server.exe"), _proc(13, "Discord.exe")
    me = _proc(10, "python.exe", [llama, sidecar, discord])
    reloader = _proc(9, "python.exe", [me, llama, sidecar, discord])
    me.parents.return_value = [reloader, _proc(8, "explorer.exe")]
    client = _proc(7, "Jarvis.App.exe")
    with patch.object(pc.psutil, "Process", return_value=me), \
         patch.object(pc.psutil, "process_iter", return_value=[client, discord]):
        assert pc.jarvis_process_ids() == {7, 9, 10, 11, 12}


def test_list_processes_reports_real_cpu_and_sorts_by_it():
    def proc(pid, name, cpu, mem_mb):
        m = _proc(pid, name)
        m.cpu_percent.side_effect = [0.0, cpu]   # psutil's first reading is always 0
        m.memory_info.return_value = MagicMock(rss=mem_mb * 1024 * 1024)
        return m

    procs = [proc(1, "big_mem.exe", 0.0, 900), proc(2, "busy.exe", 160.0, 50)]
    with patch.object(pc.psutil, "process_iter", return_value=procs), \
         patch.object(pc.psutil, "cpu_count", return_value=16), patch.object(pc.time, "sleep"):
        by_cpu = pc.list_processes(sort_by="cpu", group=False).splitlines()[2:]
    assert by_cpu[0].split()[:2] == ["2", "busy.exe"] and by_cpu[0].split()[-1] == "10.0"


def test_kill_process_closes_politely_unless_forced():
    target = _proc(4242, "notepad.exe")
    with patch.object(pc, "jarvis_process_ids", return_value=set()), \
         patch.object(pc.psutil, "process_iter", return_value=[target]):
        with patch.object(pc, "_close_windows", return_value=1) as close, \
             patch.object(pc.psutil, "wait_procs", return_value=([target], [])):
            assert pc.kill_process("notepad").startswith("Closed notepad.exe")
            close.assert_called_once_with({4242})
        with patch.object(pc, "_close_windows", return_value=1), \
             patch.object(pc.psutil, "wait_procs", return_value=([], [target])):
            assert "save changes" in pc.kill_process("notepad")
        with patch.object(pc, "_close_windows", return_value=0):
            assert "no open window" in pc.kill_process("notepad")
        target.kill.assert_not_called()
        with patch.object(pc.psutil, "wait_procs", return_value=([target], [])):
            assert pc.kill_process("notepad", force=True).startswith("Force-ended notepad.exe")
        target.kill.assert_called_once()


def test_launch_app_from_a_temp_folder_asks():
    exe = r"C:\Users\x\AppData\Local\Temp\setup_tool.exe"
    with patch("app.agent.tools.app_control.resolve_executable", return_value=exe), \
         patch("app.agent.tools.app_control.is_uri_or_shortcut", return_value=False):
        tier, reason = evaluate_launch_app_risk("setup_tool")
    assert tier == RiskTier.CONFIRMATION_REQUIRED and "temp" in reason


def test_list_processes_groups_by_app_like_task_manager():
    def proc(pid, name, cpu, mem_mb):
        m = _proc(pid, name)
        m.cpu_percent.side_effect = [0.0, cpu]
        m.memory_info.return_value = MagicMock(rss=mem_mb * 1024 * 1024)
        return m

    procs = [proc(0, "System Idle Process", 1500.0, 0)] + [proc(10 + i, "chrome.exe", 16.0, 700) for i in range(3)] \
        + [proc(2, "notepad.exe", 0.0, 20)]
    with patch.object(pc.psutil, "process_iter", return_value=procs), \
         patch.object(pc.psutil, "cpu_count", return_value=16), patch.object(pc.time, "sleep"):
        out = pc.list_processes().splitlines()
    assert out[0] == "2 apps, 4 processes (sorted by memory):"
    assert out[1] == "- chrome.exe x3: 2.1 GB, 3.0% CPU"
    assert out[2] == "- notepad.exe: 20 MB, 0.0% CPU"
