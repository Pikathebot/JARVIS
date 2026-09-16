import ctypes
import logging
import os
import re
import shutil
import subprocess
from typing import Optional, Any

try:
    import win32gui
    import win32process
    import win32con
    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

logger = logging.getLogger("jarvis.agent.tools.app_control")

APP_ALIASES: dict[str, str] = {
    "notepad": "notepad.exe",
    "calc": "calc.exe",
    "calculator": "calc.exe",
    "chrome": "chrome.exe",
    "google chrome": "chrome.exe",
    "edge": "msedge.exe",
    "msedge": "msedge.exe",
    "microsoft edge": "msedge.exe",
    "cmd": "cmd.exe",
    "command prompt": "cmd.exe",
    "powershell": "powershell.exe",
    "terminal": "wt.exe",
    "windows terminal": "wt.exe",
    "code": "code.cmd",
    "vscode": "code.cmd",
    "vs code": "code.cmd",
    "visual studio code": "code.cmd",
    "explorer": "explorer.exe",
    "file explorer": "explorer.exe",
    "paint": "mspaint.exe",
    "mspaint": "mspaint.exe",
    "taskmgr": "taskmgr.exe",
    "task manager": "taskmgr.exe",
    "spotify": "spotify.exe",
    "snipping tool": "snippingtool.exe",
    "control panel": "control.exe",
}

# Windows system apps that are not executables but URI schemes; ``os.startfile`` opens them
# the way the Start menu would.
URI_APPS: dict[str, str] = {
    "settings": "ms-settings:",
    "windows settings": "ms-settings:",
    "system settings": "ms-settings:",
    "display settings": "ms-settings:display",
    "sound settings": "ms-settings:sound",
    "bluetooth settings": "ms-settings:bluetooth",
    "bluetooth": "ms-settings:bluetooth",
    "wifi settings": "ms-settings:network-wifi",
    "network settings": "ms-settings:network",
    "windows update": "ms-settings:windowsupdate",
    "store": "ms-windows-store:",
    "microsoft store": "ms-windows-store:",
    "xbox": "xbox:",
    "camera": "microsoft.windows.camera:",
    "clock": "ms-clock:",
    "photos": "ms-photos:",
    "maps": "bingmaps:",
}

_URI_SCHEME = re.compile(r"^[a-z][a-z0-9+.\-]{1,40}:", re.IGNORECASE)


def _is_uri(text: str) -> bool:
    """A Windows app URI such as ``ms-settings:display`` -- not a drive path like ``C:\\x``."""
    if not _URI_SCHEME.match(text) or os.path.exists(text):
        return False
    scheme = text.split(":", 1)[0].lower()
    return len(scheme) > 1 and scheme not in ("http", "https", "file")


def _start_menu_dirs() -> list[str]:
    dirs = []
    for base in (os.environ.get("APPDATA"), os.environ.get("ProgramData")):
        if base:
            dirs.append(os.path.join(base, "Microsoft", "Windows", "Start Menu", "Programs"))
    return [d for d in dirs if os.path.isdir(d)]


def find_start_menu_shortcut(name: str) -> Optional[str]:
    """
    The Start Menu shortcut (``.lnk``) for an installed app, found the way a person would:
    an exact name match first ("Discord"), then a shortcut whose name starts with it, then one
    that merely contains it. Uninstall/help shortcuts are skipped so "discord" never resolves
    to "Uninstall Discord". Case-insensitive; returns None when nothing matches.
    """
    query = (name or "").strip().lower()
    if not query:
        return None
    skip = ("uninstall", "readme", "help", "license", "release notes", "documentation")
    exact: list[str] = []
    prefix: list[str] = []
    contains: list[str] = []
    for root in _start_menu_dirs():
        for dirpath, _dirnames, filenames in os.walk(root):
            for fn in filenames:
                if not fn.lower().endswith((".lnk", ".url", ".appref-ms")):
                    continue
                stem = os.path.splitext(fn)[0].lower()
                if any(word in stem for word in skip):
                    continue
                full = os.path.join(dirpath, fn)
                if stem == query:
                    exact.append(full)
                elif stem.startswith(query):
                    prefix.append(full)
                elif query in stem:
                    contains.append(full)
    for bucket in (exact, prefix, contains):
        if bucket:
            # Shortest name is the most likely primary entry ("Discord" over "Discord PTB").
            return sorted(bucket, key=lambda p: (len(os.path.basename(p)), p))[0]
    return None


def resolve_executable(name_or_path: str) -> Optional[str]:
    """
    Resolve what the user calls an app to something Windows can open: a URI scheme, an
    executable on PATH, a file path, or a Start Menu shortcut. Returns None when nothing on
    this machine matches, rather than a bare name that would only fail at launch.
    """
    clean = name_or_path.strip().strip("'\"")
    clean_lower = clean.lower()
    if not clean:
        return None

    # 1. System apps that only exist as URI schemes (Settings, Store, ...)
    if clean_lower in URI_APPS:
        return URI_APPS[clean_lower]
    if _is_uri(clean):
        return clean

    # 2. Alias lookup
    if clean_lower in APP_ALIASES:
        target = APP_ALIASES[clean_lower]
        found = shutil.which(target)
        if found:
            return found
        shortcut = find_start_menu_shortcut(clean_lower)
        if shortcut:
            return shortcut
        return target

    # 3. Direct path check
    if os.path.exists(clean):
        return clean

    # 4. PATH lookup, with and without .exe
    found = shutil.which(clean)
    if found:
        return found
    if not clean_lower.endswith(".exe"):
        found_exe = shutil.which(f"{clean}.exe")
        if found_exe:
            return found_exe

    # 5. Installed apps reachable only through the Start Menu (Discord, Steam, Epic, ...)
    shortcut = find_start_menu_shortcut(clean)
    if shortcut:
        return shortcut

    return None


def is_uri_or_shortcut(target: str) -> bool:
    return target.lower().endswith((".lnk", ".url", ".appref-ms")) or _is_uri(target)


def launch_app(name_or_path: str) -> str:
    """
    Launch a local Windows application by name, alias, or executable path.
    Requires user confirmation before execution.

    Args:
        name_or_path: Name of application (e.g. 'notepad', 'chrome', 'calculator') or full file path.
    """
    if not name_or_path or not name_or_path.strip():
        return "Error: No application name or path provided."

    target = resolve_executable(name_or_path)
    if not target:
        return (
            f"Error: Could not find an application called '{name_or_path}' on this machine "
            "(not on PATH, not in the Start Menu, and not a known system app)."
        )

    try:
        if is_uri_or_shortcut(target):
            # Shortcuts and URI schemes go through the shell, exactly as a Start Menu click would.
            os.startfile(target)
            logger.info("Launched application '%s' via shell (%s)", name_or_path, target)
            return f"Successfully launched '{name_or_path}' (via {os.path.basename(target) or target})."
        proc = subprocess.Popen(
            target,
            shell=True if target.endswith((".cmd", ".bat")) else False,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        )
        logger.info("Launched application '%s' (PID: %d)", target, proc.pid)
        return f"Successfully launched '{name_or_path}' (target: '{target}', PID: {proc.pid})."
    except FileNotFoundError:
        return f"Error: Application executable '{target}' was not found on the system."
    except Exception as e:
        logger.error("Failed to launch application '%s': %s", name_or_path, e)
        return f"Error launching application '{name_or_path}': {str(e)}"


def focus_app(name_or_title_substring: str) -> str:
    """
    Bring an existing open application window to the foreground by window title substring
    or process name. Restores minimized windows automatically.

    Args:
        name_or_title_substring: Substring of the window title or process name (e.g. 'Notepad', 'Chrome', 'Visual Studio Code').
    """
    query = (name_or_title_substring or "").strip().lower()
    if not query:
        return "Error: No window title or process name provided to focus."

    matching_hwnds: list[tuple[int, str]] = []

    if HAS_WIN32:
        def enum_windows_callback(hwnd: int, extra: Any) -> bool:
            if win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd).strip()
                if title:
                    # Check window title match
                    if query in title.lower():
                        matching_hwnds.append((hwnd, title))
                        return True

                    # Check process name match if title didn't match
                    if HAS_PSUTIL:
                        try:
                            _, pid = win32process.GetWindowThreadProcessId(hwnd)
                            proc = psutil.Process(pid)
                            pname = proc.name().lower()
                            if query in pname or query in pname.replace(".exe", ""):
                                matching_hwnds.append((hwnd, title))
                        except Exception:
                            pass
            return True

        try:
            win32gui.EnumWindows(enum_windows_callback, None)
        except Exception as e:
            logger.warning("win32gui.EnumWindows error: %s", e)

    # Fallback to ctypes if win32gui is not available or returned nothing
    if not matching_hwnds and os.name == "nt":
        user32 = ctypes.windll.user32

        def py_enum_callback(hwnd, lparam):
            if user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buff = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buff, length + 1)
                    title = buff.value.strip()
                    if query in title.lower():
                        matching_hwnds.append((hwnd, title))
            return True

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)
        cb = WNDENUMPROC(py_enum_callback)
        user32.EnumWindows(cb, 0)

    if not matching_hwnds:
        return f"Error: No visible window matching '{name_or_title_substring}' was found. Consider using launch_app to open it."

    target_hwnd, target_title = matching_hwnds[0]

    try:
        if HAS_WIN32:
            # Restore window if minimized
            if win32gui.IsIconic(target_hwnd):
                win32gui.ShowWindow(target_hwnd, win32con.SW_RESTORE)
            else:
                win32gui.ShowWindow(target_hwnd, win32con.SW_SHOW)

            # Bring to foreground
            win32gui.SetForegroundWindow(target_hwnd)
        else:
            user32 = ctypes.windll.user32
            SW_RESTORE = 9
            user32.ShowWindow(target_hwnd, SW_RESTORE)
            user32.SetForegroundWindow(target_hwnd)

        logger.info("Focused window: '%s' (HWND: %d)", target_title, target_hwnd)
        return f"Successfully focused window '{target_title}'."
    except Exception as e:
        logger.warning("Could not set foreground window directly: %s", e)
        try:
            user32 = ctypes.windll.user32
            user32.ShowWindow(target_hwnd, 9)
            user32.SetForegroundWindow(target_hwnd)
            return f"Successfully focused window '{target_title}'."
        except Exception as e2:
            return f"Error focusing window '{target_title}': {str(e2)}"
