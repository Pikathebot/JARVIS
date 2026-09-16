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
