import ctypes
import logging
import os
import re
import shutil
import subprocess
from typing import Optional, Any

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


def _start_menu_shortcuts() -> dict[str, str]:
    """Display name -> shortcut path for every app in the Start Menu, one entry per name (the
    same app often has a shortcut in both the user's and the all-users folder). Uninstall/help
    shortcuts are skipped so "discord" never resolves to "Uninstall Discord"."""
    skip = ("uninstall", "readme", "help", "license", "release notes", "documentation")
    found: dict[str, str] = {}
    seen: set[str] = set()
    for root in _start_menu_dirs():
        for dirpath, _dirnames, filenames in os.walk(root):
            for fn in sorted(filenames):
                if not fn.lower().endswith((".lnk", ".url", ".appref-ms")):
                    continue
                stem = os.path.splitext(fn)[0]
                if any(word in stem.lower() for word in skip) or stem.lower() in seen:
                    continue
                seen.add(stem.lower())
                found[stem] = os.path.join(dirpath, fn)
    return found


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def match_start_menu(name: str) -> tuple[Optional[str], list[str]]:
    """
    Find an installed app the way a person would. Returns (shortcut path, []) when one app is
    clearly meant, else (None, up to five candidate names, best first) -- "several match" or
    "closest to what was said" -- so the model can pick one or ask instead of guessing.

    Exact name first ("Discord"); else a name that starts with, then one that contains, the
    phrase, if only one does; else names sharing words with it, then near-spellings. 2026-09-29:
    "qwen studio" found nothing at all, though "Qwen" was installed.
    """
    query = " ".join((name or "").lower().split())
    if not query:
        return None, []
    apps = _start_menu_shortcuts()
    by_length = sorted(apps, key=lambda s: (len(s), s.lower()))

    for stem in by_length:
        if stem.lower() == query:
            return apps[stem], []
    for test in (lambda s: s.startswith(query), lambda s: query in s):
        hits = [s for s in by_length if test(s.lower())]
        if len(hits) == 1:
            return apps[hits[0]], []
        if hits:
            # "Discord" vs "Discord PTB": the bare name plus a variant is not a real choice.
            base = hits[0].lower()
            if all(h.lower().startswith(base + " ") for h in hits[1:]) and base.startswith(query):
                return apps[hits[0]], []
            return None, hits[:5]

    q_words = {w for w in _words(query) if len(w) >= 2}
    shared = sorted(
        ((len(q_words & _words(s)), s) for s in by_length if q_words & _words(s)),
        key=lambda t: (-t[0], len(t[1])),
    )
    candidates = [s for _, s in shared]
    import difflib
    for s in difflib.get_close_matches(query, [s.lower() for s in by_length], n=3, cutoff=0.6):
        original = next(o for o in by_length if o.lower() == s)
        if original not in candidates:
            candidates.append(original)
    return None, candidates[:5]


def find_start_menu_shortcut(name: str) -> Optional[str]:
    """The Start Menu shortcut for an installed app when one is clearly meant, else None."""
    return match_start_menu(name)[0]


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

    Args:
        name_or_path: Name of application (e.g. 'notepad', 'chrome', 'calculator') or full file path.
    """
    if not name_or_path or not name_or_path.strip():
        return "Error: No application name or path provided."

    target = resolve_executable(name_or_path)
    if not target:
        _, candidates = match_start_menu(name_or_path)
        if candidates:
            listed = ", ".join(f"'{c}'" for c in candidates)
            return (
                f"No app is called exactly '{name_or_path}'. Installed apps that could be meant: "
                f"{listed}. If one is clearly what the user asked for, call launch_app with that "
                "exact name; otherwise ask the user which one."
            )
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


def _visible_windows() -> list[tuple[int, str, str]]:
    """(hwnd, title, process name) for every visible, titled top-level window, front to back."""
    user32 = ctypes.windll.user32
    found: list[tuple[int, str, str]] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def _each(hwnd, _):
        if user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, 4):  # GW_OWNER
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buff = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buff, length + 1)
                pid = ctypes.c_ulong()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                pname = ""
                if HAS_PSUTIL:
                    try:
                        pname = psutil.Process(pid.value).name()
                    except Exception:
                        pass
                found.append((int(hwnd), buff.value.strip(), pname))
        return True

    user32.EnumWindows(_each, None)
    return found


def _bring_to_front(hwnd: int) -> bool:
    """Restore and raise a window; True only if it really is the foreground window afterwards.
    Windows' foreground lock usually refuses a background process (the backend), and then the
    window just flashes in the taskbar -- so the result is checked, never assumed."""
    user32 = ctypes.windll.user32
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    if user32.SetForegroundWindow(hwnd) and user32.GetForegroundWindow() == hwnd:
        return True
    # Second try: borrow the current foreground thread's input state.
    fg = user32.GetForegroundWindow()
    fg_thread = user32.GetWindowThreadProcessId(fg, None)
    me = ctypes.windll.kernel32.GetCurrentThreadId()
    if fg_thread and fg_thread != me and user32.AttachThreadInput(me, fg_thread, True):
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            user32.AttachThreadInput(me, fg_thread, False)
    return user32.GetForegroundWindow() == hwnd


def focus_app(name_or_title_substring: str) -> str:
    """
    Bring an already-open app's window to the front (restoring it if minimized), by process name or window title; it says honestly when Windows refused.

    Args:
        name_or_title_substring: Process name or (part of) the window title (e.g. 'Notepad', 'Chrome', 'Visual Studio Code').
    """
    query = (name_or_title_substring or "").strip().lower()
    if not query:
        return "Error: No window title or process name provided to focus."
    if os.name != "nt":
        return "Error: focus_app only works on Windows."

    q_exe = query[:-4] if query.endswith(".exe") else query
    ranked: list[tuple[int, int, str, str]] = []
    for hwnd, title, pname in _visible_windows():
        stem = pname.lower()[:-4] if pname.lower().endswith(".exe") else pname.lower()
        t = title.lower()
        # Process name, then the exact title, then part of the title, then part of the name.
        rank = 0 if stem == q_exe else 1 if t == query else 2 if query in t else 3 if q_exe and q_exe in stem else None
        if rank is not None:
            ranked.append((rank, hwnd, title, pname))
    if not ranked:
        return f"Error: No visible window matching '{name_or_title_substring}' was found. Consider using launch_app to open it."

    best = min(r[0] for r in ranked)
    top = [r for r in ranked if r[0] == best]  # stable: front-most window first
    apps = list(dict.fromkeys(r[3] or r[2] for r in top))
    if best >= 2 and len(apps) > 1:
        listed = "; ".join(f"'{r[2]}' ({r[3] or 'unknown'})" for r in top[:6])
        return f"Several windows from different apps match '{name_or_title_substring}': {listed}. Ask the user which one."

    _, hwnd, title, _ = top[0]
    try:
        ok = _bring_to_front(hwnd)
    except Exception as e:
        return f"Error focusing window '{title}': {e}"
    if ok:
        logger.info("Focused window: '%s' (HWND: %d)", title, hwnd)
        return f"Brought '{title}' to the front."
    logger.info("Windows refused to raise '%s' (HWND: %d)", title, hwnd)
    return (f"'{title}' is open, but Windows didn't let Jarvis bring it to the front -- it is "
            "probably flashing in the taskbar. Tell the user to click it there.")
