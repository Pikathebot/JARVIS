"""
App launching: names people actually say resolve to something Windows can open.
"""
import os

import pytest

from app.agent.tools import app_control
from app.agent.tools.app_control import (
    find_start_menu_shortcut,
    is_uri_or_shortcut,
    launch_app,
    resolve_executable,
)


@pytest.fixture
def start_menu(tmp_path, monkeypatch):
    programs = tmp_path / "Programs"
    (programs / "Discord Inc").mkdir(parents=True)
    (programs / "Discord Inc" / "Discord.lnk").write_bytes(b"")
    (programs / "Discord Inc" / "Uninstall Discord.lnk").write_bytes(b"")
    (programs / "Discord Inc" / "Discord PTB.lnk").write_bytes(b"")
    (programs / "Steam").mkdir()
    (programs / "Steam" / "Steam.lnk").write_bytes(b"")
    (programs / "Epic Games Launcher.lnk").write_bytes(b"")
    monkeypatch.setattr(app_control, "_start_menu_dirs", lambda: [str(programs)])
    return programs


def test_system_apps_resolve_to_uri_schemes():
    assert resolve_executable("settings") == "ms-settings:"
    assert resolve_executable("Bluetooth") == "ms-settings:bluetooth"
    assert resolve_executable("ms-settings:display") == "ms-settings:display"
    assert is_uri_or_shortcut("ms-settings:")
    assert not is_uri_or_shortcut(r"C:\Windows\notepad.exe")


def test_start_menu_search_prefers_exact_then_prefix_then_contains_and_skips_uninstallers(start_menu):
    assert find_start_menu_shortcut("discord").endswith("Discord.lnk")
    assert find_start_menu_shortcut("epic").endswith("Epic Games Launcher.lnk")
    assert find_start_menu_shortcut("games launcher").endswith("Epic Games Launcher.lnk")
    assert find_start_menu_shortcut("uninstall") is None
    assert find_start_menu_shortcut("fortnite") is None


def test_installed_app_resolves_through_the_start_menu(start_menu):
    assert resolve_executable("Discord").endswith("Discord.lnk")
    assert resolve_executable("steam").endswith("Steam.lnk")


def test_unknown_app_resolves_to_none_not_a_bare_name(start_menu):
    assert resolve_executable("no-such-app-anywhere") is None
    assert launch_app("no-such-app-anywhere").startswith("Error: Could not find an application")


def test_shortcuts_and_uris_launch_through_the_shell(start_menu, monkeypatch):
    opened = []
    monkeypatch.setattr(app_control.os, "startfile", lambda target: opened.append(target), raising=False)

    assert launch_app("discord").startswith("Successfully launched 'discord'")
    assert launch_app("settings").startswith("Successfully launched 'settings'")
    assert opened[0].endswith("Discord.lnk") and opened[1] == "ms-settings:"


@pytest.mark.skipif(os.name != "nt", reason="Windows PATH lookup")
def test_path_executables_still_resolve():
    assert resolve_executable("notepad").lower().endswith("notepad.exe")


# --- matching what people say, not only exact names (2026-09-29: "qwen studio" found nothing) ---

@pytest.fixture
def busy_start_menu(start_menu):
    for name in ("Qwen", "Unsloth Studio", "Android Studio", "Visual Studio Code"):
        (start_menu / f"{name}.lnk").write_bytes(b"")
    (start_menu / "Steam" / "Steam.lnk").write_bytes(b"")
    return start_menu


def test_a_phrase_with_no_exact_app_returns_the_closest_ones(busy_start_menu):
    path, candidates = app_control.match_start_menu("qwen studio")
    assert path is None
    assert set(candidates[:3]) == {"Qwen", "Unsloth Studio", "Android Studio"}
    out = launch_app("qwen studio")
    assert "'Qwen'" in out and "ask the user" in out


def test_several_matches_and_none_exact_ask_instead_of_guessing(busy_start_menu):
    path, candidates = app_control.match_start_menu("studio")
    assert path is None and "Android Studio" in candidates and "Unsloth Studio" in candidates
    # A bare name plus a variant of it is not a real choice.
    assert app_control.match_start_menu("disc")[0].endswith("Discord.lnk")


def test_typos_suggest_the_near_spelling(busy_start_menu):
    path, candidates = app_control.match_start_menu("discrod")
    assert path is None and candidates[0] == "Discord"


def test_focus_app_prefers_the_process_and_admits_when_windows_refuses(monkeypatch):
    windows = [
        (1, "notes.txt - Notepad", "Notepad.exe"),
        (2, "How to use notepad - Chrome", "chrome.exe"),
    ]
    monkeypatch.setattr(app_control, "_visible_windows", lambda: windows)
    monkeypatch.setattr(app_control.os, "name", "nt")
    raised = []
    monkeypatch.setattr(app_control, "_bring_to_front", lambda hwnd: raised.append(hwnd) or True)
    assert app_control.focus_app("notepad").startswith("Brought 'notes.txt - Notepad'")
    assert raised == [1]

    monkeypatch.setattr(app_control, "_bring_to_front", lambda hwnd: False)
    assert "didn't let Jarvis" in app_control.focus_app("notepad")

    windows[:] = [(3, "Project plan - Word", "WINWORD.EXE"), (4, "Project plan - Chrome", "chrome.exe")]
    assert app_control.focus_app("project plan").startswith("Several windows")
