"""
What is installed on this PC: games from the launchers' own records, and the Start menu's apps.

"What games do I have?" took four turns and a dozen guesses on 2026-10-23 (PLAN 4.23c): the
model searched AppData and C:\\ for .exe files. The launchers already keep a list -- Steam an
``appmanifest_<id>.acf`` per game in each library's ``steamapps`` folder (libraries listed in
``libraryfolders.vdf``), Epic a JSON ``.item`` per game, GOG a registry key per game -- so this
reads those, read-only, and never walks a drive.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
from typing import Optional

logger = logging.getLogger("jarvis.agent.tools.installed")

# Steam ships these as "apps" beside the games.
_STEAM_NOT_GAMES = re.compile(r"redistributable|steamworks|proton|steam linux runtime|steamvr|soundtrack|dedicated server", re.I)
_VDF_PATH = re.compile(r'"path"\s+"([^"]+)"', re.I)
_ACF_FIELD = re.compile(r'"(appid|name|SizeOnDisk)"\s+"([^"]*)"', re.I)


def _registry_value(root, key: str, name: str) -> Optional[str]:
    try:
        import winreg

        with winreg.OpenKey(root, key) as k:
            value, _ = winreg.QueryValueEx(k, name)
            return str(value)
    except Exception:
        return None


def steam_root() -> Optional[str]:
    candidates = []
    try:
        import winreg

        candidates.append(_registry_value(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"))
        candidates.append(_registry_value(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"))
    except ImportError:
        pass
    candidates.append(os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "Steam"))
    for c in candidates:
        if c and os.path.isdir(os.path.join(c, "steamapps")):
            return os.path.normpath(c)
    return None


def steam_libraries(root: str) -> list[str]:
    libraries = [root]
    try:
        with open(os.path.join(root, "steamapps", "libraryfolders.vdf"), encoding="utf-8", errors="replace") as f:
            for raw in _VDF_PATH.findall(f.read()):
                path = os.path.normpath(raw.replace("\\\\", "\\"))
                if path.lower() not in (p.lower() for p in libraries):
                    libraries.append(path)
    except OSError:
        pass
    return [p for p in libraries if os.path.isdir(os.path.join(p, "steamapps"))]


def steam_games() -> list[tuple[str, int, str]]:
    """(name, size in bytes, library) for each installed Steam game."""
    root = steam_root()
    if not root:
        return []
    games = []
    for library in steam_libraries(root):
        for manifest in glob.glob(os.path.join(library, "steamapps", "appmanifest_*.acf")):
            try:
                with open(manifest, encoding="utf-8", errors="replace") as f:
                    fields = {k.lower(): v for k, v in _ACF_FIELD.findall(f.read())}
            except OSError:
                continue
            name = fields.get("name", "").strip()
            if not name or _STEAM_NOT_GAMES.search(name):
                continue
            try:
                size = int(fields.get("sizeondisk", "0"))
            except ValueError:
                size = 0
            games.append((name, size, library))
    return sorted(games, key=lambda g: g[0].lower())


def epic_games() -> list[tuple[str, str]]:
    """(name, install folder) from the Epic launcher's manifests."""
    folder = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "Epic", "EpicGamesLauncher", "Data", "Manifests")
    games = []
    for item in glob.glob(os.path.join(folder, "*.item")):
        try:
            with open(item, encoding="utf-8", errors="replace") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        name = str(data.get("DisplayName") or "").strip()
        categories = data.get("AppCategories") or []
        # A game, not its DLC or extras (they name a main game) and not engine plugins (no "games").
        if name and "games" in categories and "digitalextras" not in categories and not data.get("MainGameAppName"):
            games.append((name, str(data.get("InstallLocation") or "")))
    return sorted(games, key=lambda g: g[0].lower())


def gog_games() -> list[str]:
    try:
        import winreg
    except ImportError:
        return []
    names = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\GOG.com\Games") as games:
            for i in range(winreg.QueryInfoKey(games)[0]):
                sub = winreg.EnumKey(games, i)
                name = _registry_value(winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\GOG.com\Games\{sub}", "gameName")
                if name:
                    names.append(name)
    except OSError:
        pass
    return sorted(names, key=str.lower)


def _gb(size: int) -> str:
    return f"{size / 1024 ** 3:.1f} GB" if size else "size unknown"


def installed_games() -> str:
    steam, epic, gog = steam_games(), epic_games(), gog_games()
    lines = []
    if steam:
        lines.append(f"Steam ({len(steam)}):")
        lines += [f"- {name} ({_gb(size)}, {library})" for name, size, library in steam]
    if epic:
        lines.append(f"Epic Games ({len(epic)}):")
        lines += [f"- {name}" + (f" ({where})" if where else "") for name, where in epic]
    if gog:
        lines.append(f"GOG ({len(gog)}):")
        lines += [f"- {name}" for name in gog]
    total = len(steam) + len(epic) + len(gog)
    head = f"Installed games: {total}, from the Steam, Epic and GOG launchers' own records." if total else (
        "No games found in the Steam, Epic or GOG launchers' records.")
    note = ("Games from other launchers (EA app, Xbox, Ubisoft) or installed on their own aren't in these records; "
            "get_system_status(about='apps') lists every app in the Start menu.")
    return "\n".join([head, *lines, note])


def installed_apps() -> str:
    from app.agent.tools.app_control import _start_menu_shortcuts

    names = sorted(_start_menu_shortcuts(), key=str.lower)
    if not names:
        return "No apps found in the Start menu."
    return f"Start menu apps ({len(names)}): " + ", ".join(names)
