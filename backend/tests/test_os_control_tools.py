import inspect
import os
import sys
import uuid
import pytest
from unittest.mock import patch, MagicMock

import psutil

from app.agent.permissions import (
    RiskTier,
    MAJOR_PROCESS_NAMES,
    evaluate_tool_permission,
    evaluate_tool_calls_batch,
    generate_action_id,
    evaluate_kill_process_risk,
)
from app.agent.tools.registry import (
    AVAILABLE_TOOLS,
    TOOL_FUNCTIONS,
    TOOL_SCHEMAS,
    execute_tool,
)
from app.agent.tools.app_control import launch_app, focus_app, resolve_executable
from app.agent.tools.media_control import set_volume, set_mute, media_control
from app.agent.tools.clipboard_control import get_clipboard, set_clipboard
from app.agent.tools.process_control import list_processes, kill_process
from app.agent.tools.notify import send_toast


# 1. Tool Registration Checks

def test_all_phase3_tools_registered():
    """Verify all 10 tools are registered in TOOL_FUNCTIONS, AVAILABLE_TOOLS, and TOOL_SCHEMAS."""
    expected_tools = [
        "launch_app", "focus_app", "set_volume", "set_mute", "media_control",
        "get_clipboard", "set_clipboard", "list_processes", "kill_process", "remind_me", "reminders"
    ]
    registered_func_names = [fn.__name__ for fn in AVAILABLE_TOOLS]
    for tool_name in expected_tools:
        assert tool_name in TOOL_FUNCTIONS, f"{tool_name} missing from TOOL_FUNCTIONS"
        assert tool_name in registered_func_names, f"{tool_name} missing from AVAILABLE_TOOLS"
        assert tool_name in TOOL_SCHEMAS, f"{tool_name} missing from TOOL_SCHEMAS"


# 2. Permission Tiering & Safety Checks

def test_launch_app_by_name_is_allowed_but_arbitrary_binaries_still_ask():
    """Opening an installed app by name is what the user asked for; running an explicit path is not."""
    decision = evaluate_tool_permission("launch_app", {"name_or_path": "notepad"})
    assert decision.allowed is True
    assert decision.risk_tier == RiskTier.LOW_RISK

    decision = evaluate_tool_permission("launch_app", {"name_or_path": "C:/Users/me/Downloads/installer.exe"})
    assert decision.allowed is False
    assert decision.risk_tier == RiskTier.CONFIRMATION_REQUIRED


def test_focus_app_low_risk():
    """focus_app must be LOW_RISK (no new execution, non-destructive)."""
    decision = evaluate_tool_permission("focus_app", {"name_or_title_substring": "notepad"})
    assert decision.allowed is True
    assert decision.risk_tier == RiskTier.LOW_RISK


def test_media_controls_low_risk():
    """Volume, mute, and media keys must all be LOW_RISK."""
    for tool, args in [
        ("set_volume", {"level": 50}),
        ("set_mute", {"on": True}),
        ("media_control", {"action": "play_pause"}),
    ]:
        decision = evaluate_tool_permission(tool, args)
        assert decision.allowed is True, f"{tool} should be allowed"
        assert decision.risk_tier == RiskTier.LOW_RISK, f"{tool} should be LOW_RISK"


def test_clipboard_reads_run_when_asked_and_writes_always_run():
    """Reading asks unless the user pointed at the clipboard; writing is like pressing Ctrl+C."""
    for message in ("what's on my clipboard?", "summarize what I copied", "fix the code I pasted"):
        assert evaluate_tool_permission("get_clipboard", {}, user_message=message).allowed, message
    unprompted = evaluate_tool_permission("get_clipboard", {}, user_message="open discord")
    assert unprompted.allowed is False and "private" in unprompted.reason
    # Outside a chat turn (no message known) it asks.
    assert evaluate_tool_permission("get_clipboard", {}).allowed is False

    dec_set = evaluate_tool_permission("set_clipboard", {"text": "hello"})
    assert dec_set.allowed is True and dec_set.risk_tier == RiskTier.LOW_RISK


def test_list_processes_low_risk():
    """list_processes is read-only and must be LOW_RISK."""
    decision = evaluate_tool_permission("list_processes", {})
    assert decision.allowed is True
    assert decision.risk_tier == RiskTier.LOW_RISK


def test_reminders_are_low_risk():
    """Setting a reminder only schedules something Jarvis will say."""
    decision = evaluate_tool_permission("remind_me", {"text": "Stretch", "when": "in 5 minutes"})
    assert decision.allowed is True
    assert decision.risk_tier == RiskTier.LOW_RISK


def test_kill_process_ordinary_single_still_asks():
    """Even a single ordinary process asks: killing it loses its unsaved work."""
    with patch("psutil.process_iter") as mock_iter:
        mock_proc = MagicMock()
        mock_proc.info = {"pid": 99999, "name": "dummy_calculator.exe"}
        mock_iter.return_value = [mock_proc]

        tier, reason = evaluate_kill_process_risk("dummy_calculator.exe")
        assert tier == RiskTier.CONFIRMATION_REQUIRED

        dec = evaluate_tool_permission("kill_process", {"pid_or_name": "dummy_calculator.exe"})
        assert "may ask to save" in dec.reason
        forced = evaluate_tool_permission("kill_process", {"pid_or_name": "dummy_calculator.exe", "force": True})
        assert forced.allowed is False and "unsaved is lost" in forced.reason
        assert dec.allowed is False
        assert dec.risk_tier == RiskTier.CONFIRMATION_REQUIRED


def test_kill_process_multi_instance_confirmation_required():
    """Terminating a process name matching >1 running instances dynamically escalates to CONFIRMATION_REQUIRED."""
    with patch("psutil.process_iter") as mock_iter:
        mock_p1 = MagicMock()
        mock_p1.info = {"pid": 1001, "name": "notepad.exe"}
        mock_p2 = MagicMock()
        mock_p2.info = {"pid": 1002, "name": "notepad.exe"}
        mock_iter.return_value = [mock_p1, mock_p2]

        tier, reason = evaluate_kill_process_risk("notepad.exe")
        assert tier == RiskTier.CONFIRMATION_REQUIRED
        assert "matches 2 running instances" in (reason or "")

        dec = evaluate_tool_permission("kill_process", {"pid_or_name": "notepad.exe"})
        assert dec.allowed is False
        assert dec.risk_tier == RiskTier.CONFIRMATION_REQUIRED


def test_kill_process_major_confirmation_required():
    """Major/critical system processes in MAJOR_PROCESS_NAMES must require confirmation."""
    for major_name in ["explorer.exe", "svchost.exe", "winlogon.exe", "dwm.exe", "System", "smss.exe"]:
        tier, reason = evaluate_kill_process_risk(major_name)
        assert tier == RiskTier.CONFIRMATION_REQUIRED, f"{major_name} should be CONFIRMATION_REQUIRED"
        
        dec = evaluate_tool_permission("kill_process", {"pid_or_name": major_name})
        assert dec.allowed is False
        assert dec.risk_tier == RiskTier.CONFIRMATION_REQUIRED


def test_kill_process_protects_jarvis_self():
    """kill_process must hard-block killing Jarvis's own backend process by PID or matching name."""
    self_pid = str(os.getpid())
    result_pid = kill_process(self_pid)
    assert "part of Jarvis itself" in result_pid

    # Test name-based self protection
    with patch("psutil.process_iter") as mock_iter:
        mock_self_proc = MagicMock()
        mock_self_proc.pid = os.getpid()
        mock_self_proc.info = {"pid": os.getpid(), "name": "jarvis_backend.exe"}
        mock_iter.return_value = [mock_self_proc]

        result_name = kill_process("jarvis_backend.exe")
        assert "part of Jarvis itself" in result_name


# 3. Tool Functionality & Robustness Checks

def test_app_control_resolve_executable():
    """resolve_executable accurately translates aliases."""
    assert resolve_executable("notepad") is not None
    assert resolve_executable("calc") is not None
    assert resolve_executable("cmd") is not None


def test_launch_app_execution():
    """launch_app spawns a process or handles invalid binary names gracefully."""
    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.pid = 4321
        mock_popen.return_value = mock_proc

        res = launch_app("notepad")
        assert "Successfully launched" in res
        assert "PID: 4321" in res


def test_focus_app_missing():
    """focus_app returns descriptive error when target window is not open."""
    res = focus_app("NonExistentSuperFakeWindowTitle12345")
    assert "Error: No visible window matching" in res


def test_set_volume_absolute_and_relative_report_old_and_new():
    with patch("app.agent.tools.media_control._get_endpoint_volume") as mock_vol_getter:
        mock_vol = MagicMock()
        mock_vol.GetMasterVolumeLevelScalar.return_value = 0.4
        mock_vol.GetMute.return_value = False
        mock_vol_getter.return_value = mock_vol

        assert set_volume(level=150) == "Volume 40% -> 100%."
        mock_vol.SetMasterVolumeLevelScalar.assert_called_with(1.0, None)
        assert set_volume(change=10) == "Volume 40% -> 50%."
        assert set_volume(change=-60) == "Volume 40% -> 0%."
        mock_vol.GetMute.return_value = True
        assert "muted" in set_volume(change=5)
        assert set_volume().startswith("Error")


def test_set_mute_sets_the_state_and_says_it():
    with patch("app.agent.tools.media_control._get_endpoint_volume") as mock_vol_getter:
        mock_vol = MagicMock()
        state = {"muted": False}
        mock_vol.GetMute.side_effect = lambda: state["muted"]
        mock_vol.SetMute.side_effect = lambda on, _: state.update(muted=on)
        mock_vol_getter.return_value = mock_vol

        assert set_mute(True) == "Sound is muted."
        assert set_mute(True) == "Sound is muted (it already was)."
        assert set_mute(False) == "Sound is unmuted."


def test_media_control_uses_the_session_and_falls_back_to_keys():
    import app.agent.tools.media_control as mc

    async def session(action):
        return {"status": "Playing in Spotify: Song -- Artist.", "pause": "Paused in Spotify: Song -- Artist."}[action]

    with patch.object(mc, "_session_action", session), patch.object(mc, "_send_virtual_key") as keys:
        assert media_control("status") == "Playing in Spotify: Song -- Artist."
        assert media_control("pause").startswith("Paused")
        keys.assert_not_called()

    async def broken(action):
        raise OSError("no WinRT")

    with patch.object(mc, "_session_action", broken), patch.object(mc, "_send_virtual_key") as keys:
        assert media_control("next").startswith("Sent the 'next' media key")
        keys.assert_called_once()
        assert "Can't tell" in media_control("status")
    assert media_control("destroy_computer").startswith("Error: Unknown media action")
    assert mc._app_name("SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify") == "Spotify"
    assert mc._app_name("chrome.exe") == "chrome"


def test_clipboard_read_write():
    """set_clipboard and get_clipboard work with text."""
    test_str = f"jarvis_test_string_{uuid.uuid4().hex[:8]}"
    with patch("pyperclip.copy") as mock_copy, patch("pyperclip.paste") as mock_paste:
        mock_paste.return_value = test_str

        set_res = set_clipboard(test_str)
        assert "Successfully copied" in set_res
        mock_copy.assert_called_with(test_str)

        get_res = get_clipboard()
        assert get_res == test_str


def test_list_processes_execution():
    """list_processes returns a formatted table containing PID and Name."""
    output = list_processes()
    assert "PID" in output
    assert "Name" in output
    assert "Memory (MB)" in output


def test_send_toast_execution():
    """send_toast executes without error and handles urgent flag."""
    with patch("app.agent.tools.notify.HAS_WINOTIFY", False), \
         patch("subprocess.run") as mock_sp:
        mock_sp.return_value = MagicMock(returncode=0)

        res = send_toast("Test Title", "Test Message", urgent=True)
        assert "Notification displayed" in res
        assert "[URGENT] Test Title" in res


# 4. Strict Governor Scaffolding Isolation Check

def test_no_governor_scaffolding_imported():
    """Confirm process_control.py contains NO import of or call into process_watcher.py or governor scaffolding."""
    import app.agent.tools.process_control as pc
    source = inspect.getsource(pc)
    assert "process_watcher" not in source
    assert "resource_governor" not in source
    assert "app.governor" not in source


def test_audio_initialises_com_once_on_each_worker_thread():
    """Tools run on a thread pool; pycaw on a thread without COM failed with "CoInitialize has
    not been called" (on device, 2026-10-01, intermittently)."""
    import threading
    import comtypes
    import app.agent.tools.media_control as mc

    calls = []
    with patch.object(comtypes, "CoInitializeEx", side_effect=lambda *a: calls.append(threading.get_ident())):
        def worker():
            mc._ensure_com()
            mc._ensure_com()
        threads = [threading.Thread(target=worker) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert len(calls) == 3 and len(set(calls)) == 3
