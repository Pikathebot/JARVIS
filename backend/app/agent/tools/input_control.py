"""
Keyboard and mouse input into whatever window has focus -- the hands of voice mode (PLAN 4.9).

``type_text``, ``press_keys`` and ``scroll`` are model tools too. What they touch is the
*focused* window, so the permission gate asks first when that is a terminal or the Run box (a
typed line plus Enter there is a command) and for shortcuts that throw work away
(``foreground_is_risky`` / ``DESTRUCTIVE_KEYS``). ``open_folder`` and ``system_action`` serve
voice mode's direct plans only.

All input goes through ``SendInput``: typed text as Unicode key events (layout-independent), so
"café" or "₹" types as said.
"""
from __future__ import annotations

import ctypes
import logging
import os
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("jarvis.agent.tools.input")

_user32 = ctypes.WinDLL("user32", use_last_error=True) if os.name == "nt" else None

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_KEYUP, KEYEVENTF_UNICODE, KEYEVENTF_EXTENDEDKEY = 0x0002, 0x0004, 0x0001
MOUSEEVENTF_WHEEL = 0x0800
WHEEL_DELTA = 120

ULONG_PTR = ctypes.c_size_t


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


VK = {
    "ctrl": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B, "enter": 0x0D, "escape": 0x1B, "tab": 0x09,
    "backspace": 0x08, "delete": 0x2E, "space": 0x20, "home": 0x24, "end": 0x23, "pageup": 0x21,
    "pagedown": 0x22, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "f4": 0x73, "f5": 0x74,
    "f6": 0x75, "f11": 0x7A, "plus": 0xBB, "minus": 0xBD,
}
_EXTENDED = {0x2E, 0x24, 0x23, 0x21, 0x22, 0x25, 0x26, 0x27, 0x28, 0x5B}

# Named shortcuts (the voice brain's SHORTCUTS keys) -> key combination.
SHORTCUT_KEYS: dict[str, str] = {
    "enter": "enter", "escape": "escape", "tab": "tab", "backspace": "backspace",
    "delete_word": "ctrl+backspace", "copy": "ctrl+c", "paste": "ctrl+v", "cut": "ctrl+x",
    "undo": "ctrl+z", "redo": "ctrl+y", "select_all": "ctrl+a", "save": "ctrl+s", "find": "ctrl+f",
    "new_tab": "ctrl+t", "close_tab": "ctrl+w", "reopen_tab": "ctrl+shift+t", "next_tab": "ctrl+tab",
    "previous_tab": "ctrl+shift+tab", "back": "alt+left", "forward": "alt+right", "reload": "f5",
    "address_bar": "ctrl+l", "close_window": "alt+f4", "minimize": "win+down", "maximize": "win+up",
    "switch_app": "alt+tab", "zoom_in": "ctrl+plus", "zoom_out": "ctrl+minus",
    "show_desktop": "win+d", "top": "ctrl+home", "bottom": "ctrl+end",
}
# Shortcuts that throw work away or close things: the gate asks before these.
DESTRUCTIVE_KEYS = frozenset({"close_tab", "close_window", "cut", "alt+f4", "ctrl+w", "ctrl+x"})

# Focused windows where typed text + Enter is a command, not prose.
_RISKY_PROCESSES = frozenset({
    "windowsterminal.exe", "cmd.exe", "powershell.exe", "pwsh.exe", "conhost.exe", "openconsole.exe",
    "mintty.exe", "wsl.exe", "bash.exe", "powershell_ise.exe", "regedit.exe",
})


def _send(inputs: list[_INPUT]) -> None:
    if _user32 is None:
        raise OSError("keyboard and mouse input needs Windows")
    arr = (_INPUT * len(inputs))(*inputs)
    sent = _user32.SendInput(len(inputs), arr, ctypes.sizeof(_INPUT))
    if sent != len(inputs):
        raise OSError(f"SendInput delivered {sent}/{len(inputs)} events (blocked by an elevated window?)")


def _key(vk: int, up: bool) -> _INPUT:
    flags = (KEYEVENTF_KEYUP if up else 0) | (KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED else 0)
    return _INPUT(type=INPUT_KEYBOARD, u=_INPUTUNION(ki=_KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=0)))


def _unicode(ch: str, up: bool) -> _INPUT:
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
    return _INPUT(type=INPUT_KEYBOARD, u=_INPUTUNION(ki=_KEYBDINPUT(wVk=0, wScan=ord(ch), dwFlags=flags, time=0, dwExtraInfo=0)))


def parse_combo(combo: str) -> list[int]:
    """'ctrl+shift+t' -> virtual-key codes in press order. Raises ValueError on an unknown key."""
    codes = []
    for part in combo.lower().replace(" ", "").split("+"):
        if part in VK:
            codes.append(VK[part])
        elif len(part) == 1 and (part.isalnum()):
            codes.append(ord(part.upper()))
        else:
            raise ValueError(f"unknown key '{part}'")
    if not codes:
        raise ValueError("no keys given")
    return codes


def _press_combo(combo: str) -> None:
    codes = parse_combo(combo)
    _send([_key(c, False) for c in codes] + [_key(c, True) for c in reversed(codes)])


def foreground_window() -> tuple[str, str]:
    """(process exe name, window title) of the focused window; ('', '') when unknown."""
    if _user32 is None:
        return "", ""
    try:
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return "", ""
        length = _user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buf, length + 1)
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        import psutil

        name = psutil.Process(pid.value).name()
        return name, buf.value
    except Exception as e:
        logger.debug("foreground window lookup failed: %s", e)
        return "", ""


def foreground_is_risky() -> Optional[str]:
    """Why input into the focused window needs a yes first, or None."""
    name, title = foreground_window()
    low = name.lower()
    if low in _RISKY_PROCESSES:
        return f"The focused window is a terminal ({name}): typed text and Enter run as a command."
    if low == "explorer.exe" and title.strip().lower() == "run":
        return "The focused window is the Run box: Enter runs what is typed."
    return None


# --- one particular window (voice mode's "close it": PLAN 4.9) ---
#
# A yes can come many seconds after the question, and focus moves in between (on device, "close
# it" asked about one window while the yes pressed Alt+F4 in whatever was in front by then). So a
# shortcut that needs a yes is pinned to the window it was asked about, named in the question,
# and closing goes to that window by handle instead of through the keyboard.

_OWN_PROCESSES = frozenset({"jarvis.app.exe"})
_GW_HWNDNEXT, _WM_CLOSE, _GWL_EXSTYLE, _WS_EX_TOOLWINDOW, _DWMWA_CLOAKED = 2, 0x0010, -20, 0x00000080, 14


@dataclass(frozen=True)
class Window:
    hwnd: int
    exe: str
    title: str

    @property
    def label(self) -> str:
        """What to call it out loud: the app part of "file - Visual Studio Code", else the title."""
        if " - " in self.title:
            return self.title.rsplit(" - ", 1)[1].strip()
        return self.title.strip() or self.exe.removesuffix(".exe").removesuffix(".EXE")


def _window(hwnd: int) -> Optional[Window]:
    try:
        length = _user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buf, length + 1)
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        import psutil

        return Window(int(hwnd), psutil.Process(pid.value).name(), buf.value)
    except Exception as e:
        logger.debug("window lookup failed: %s", e)
        return None


def _is_app_window(hwnd: int) -> bool:
    """Visible, titled, not a tool window and not cloaked (UWP keeps hidden windows 'visible')."""
    if not _user32.IsWindowVisible(hwnd) or _user32.GetWindowTextLengthW(hwnd) == 0:
        return False
    if _user32.GetWindowLongW(hwnd, _GWL_EXSTYLE) & _WS_EX_TOOLWINDOW:
        return False
    cloaked = wintypes.DWORD()
    try:
        ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, _DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
    except Exception:
        pass
    return not cloaked.value


def target_window() -> Optional[Window]:
    """The window a shortcut is meant for: the focused one, or -- when that is Jarvis's own
    (the user just used the mic or the HUD) -- the app window right under it."""
    if _user32 is None:
        return None
    hwnd = _user32.GetForegroundWindow()
    for _ in range(64):
        if not hwnd:
            return None
        if _is_app_window(hwnd):
            w = _window(hwnd)
            if w and w.exe.lower() not in _OWN_PROCESSES:
                return w
        hwnd = _user32.GetWindow(hwnd, _GW_HWNDNEXT)
    return None


def window_exists(w: Window) -> bool:
    return bool(_user32 and _user32.IsWindow(w.hwnd))


def close_window(w: Window) -> str:
    """Ask exactly this window to close (WM_CLOSE: as its X button; unsaved work still prompts)."""
    if not window_exists(w):
        return f"{w.label} is already closed."
    if not _user32.PostMessageW(w.hwnd, _WM_CLOSE, 0, 0):
        return f"Error: couldn't close {w.label} (error {ctypes.get_last_error()})."
    return f"Closed {w.label}."


def bring_to_front(w: Window, timeout: float = 1.0) -> bool:
    """Put ``w`` in front again (the backend holds a foreground grant from the client's last
    voice request). True once it is the focused window."""
    if not window_exists(w):
        return False
    _user32.SetForegroundWindow(w.hwnd)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _user32.GetForegroundWindow() == w.hwnd:
            return True
        time.sleep(0.05)
    return False


def type_text(text: str, press_enter: bool = False) -> str:
    """
    Type text into the focused window as if typed on the keyboard -- only when the user asks for text to be typed or dictated.

    Args:
        text: Exactly the text to type.
        press_enter: Press Enter afterwards (when the user says "and hit enter" / "and send it").
    """
    if not text:
        return "Error: no text to type."
    name, title = foreground_window()
    try:
        events = []
        for ch in text.replace("\r\n", "\n"):
            if ch == "\n":
                events += [_key(VK["enter"], False), _key(VK["enter"], True)]
            elif ord(ch) > 0xFFFF:  # outside the BMP: a UTF-16 surrogate pair
                b = ch.encode("utf-16-le")
                for i in range(0, 4, 2):
                    unit = chr(int.from_bytes(b[i:i + 2], "little"))
                    events += [_unicode(unit, False), _unicode(unit, True)]
            else:
                events += [_unicode(ch, False), _unicode(ch, True)]
        for i in range(0, len(events), 200):  # big pastes in slices; some apps drop a flood
            _send(events[i:i + 200])
            if len(events) > 200:
                time.sleep(0.01)
        if press_enter:
            _press_combo("enter")
    except Exception as e:
        return f"Error: typing failed: {e}"
    where = f" into {title or name}" if (title or name) else ""
    return f"Typed {len(text)} characters{where}" + (" and pressed Enter." if press_enter else ".")


def press_keys(keys: str) -> str:
    """
    Press a key or keyboard shortcut in the focused window: a name (copy, paste, undo, save, new_tab, close_tab, back, reload, select_all, enter, escape, ...) or a combination like "ctrl+shift+t".

    Args:
        keys: A shortcut name or a '+'-joined key combination.
    """
    name = (keys or "").strip().lower().replace(" ", "_")
    combo = SHORTCUT_KEYS.get(name, name.replace("_", ""))
    try:
        _press_combo(combo)
    except ValueError as e:
        return f"Error: {e}. Use a shortcut name ({', '.join(sorted(SHORTCUT_KEYS))}) or keys like ctrl+shift+t."
    except Exception as e:
        return f"Error: pressing {combo} failed: {e}"
    return f"Pressed {combo}."


SCROLL_NOTCHES = {"little": 3, "page": 10, "a_lot": 30}


def scroll(direction: str = "down", amount: str = "page") -> str:
    """
    Scroll the window under the mouse up or down, or jump to the top or bottom.

    Args:
        direction: down, up, top or bottom.
        amount: little, page or a_lot (ignored for top/bottom).
    """
    d = (direction or "down").strip().lower()
    try:
        if d in ("top", "bottom"):
            _press_combo(SHORTCUT_KEYS[d])
            return f"Jumped to the {d}."
        if d not in ("up", "down"):
            return "Error: direction must be up, down, top or bottom."
        notches = SCROLL_NOTCHES.get((amount or "page").strip().lower(), 10)
        delta = WHEEL_DELTA * notches * (1 if d == "up" else -1)
        mi = _MOUSEINPUT(dx=0, dy=0, mouseData=ctypes.c_uint32(delta).value, dwFlags=MOUSEEVENTF_WHEEL, time=0, dwExtraInfo=0)
        _send([_INPUT(type=INPUT_MOUSE, u=_INPUTUNION(mi=mi))])
    except Exception as e:
        return f"Error: scrolling failed: {e}"
    return f"Scrolled {d}."


_SHELL_FOLDERS = {
    "downloads": "shell:Downloads", "documents": "shell:Personal", "desktop": "shell:Desktop",
    "pictures": "shell:My Pictures", "music": "shell:My Music", "videos": "shell:My Video",
    "home": "shell:Profile", "this pc": "shell:MyComputerFolder",
}


def open_folder(folder: str) -> str:
    target = _SHELL_FOLDERS.get((folder or "").strip().lower())
    if not target:
        return f"Error: unknown folder '{folder}'."
    try:
        os.startfile(target)  # type: ignore[attr-defined]
    except Exception as e:
        return f"Error: could not open {folder}: {e}"
    return f"Opened {folder}."


def system_action(op: str) -> str:
    try:
        if op == "lock":
            if not _user32.LockWorkStation():
                return "Error: Windows refused to lock."
            return "Locked."
        if op == "show_desktop":
            _press_combo("win+d")
            return "Showing the desktop."
        if op == "screen_off":
            HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER = 0xFFFF, 0x0112, 0xF170
            _user32.PostMessageW(HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER, 2)
            return "Screen off."
        if op == "screenshot":
            from PIL import ImageGrab

            folder = os.path.join(os.path.expanduser("~"), "Pictures", "Screenshots")
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, time.strftime("Jarvis %Y-%m-%d %H%M%S.png"))
            ImageGrab.grab(all_screens=True).save(path)
            return f"Saved a screenshot to {path}."
    except Exception as e:
        return f"Error: {op} failed: {e}"
    return f"Error: unknown system action '{op}'."
