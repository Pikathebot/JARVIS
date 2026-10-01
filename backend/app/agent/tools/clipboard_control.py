import ctypes
import logging

try:
    import pyperclip
    HAS_PYPERCLIP = True
except ImportError:
    HAS_PYPERCLIP = False

logger = logging.getLogger("jarvis.agent.tools.clipboard_control")


def _get_clipboard_win32() -> str:
    """Fallback to Win32 ctypes API for reading clipboard text."""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    CF_UNICODETEXT = 13
    if not user32.OpenClipboard(0):
        raise RuntimeError("Failed to open Windows clipboard.")
    try:
        h_glb = user32.GetClipboardData(CF_UNICODETEXT)
        if not h_glb:
            return ""
        p_glb = kernel32.GlobalLock(h_glb)
        if not p_glb:
            return ""
        try:
            text = ctypes.c_wchar_p(p_glb).value or ""
            return text
        finally:
            kernel32.GlobalUnlock(h_glb)
    finally:
        user32.CloseClipboard()


def _set_clipboard_win32(text: str) -> None:
    """Fallback to Win32 ctypes API for writing clipboard text."""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002

    if not user32.OpenClipboard(0):
        raise RuntimeError("Failed to open Windows clipboard.")
    try:
        user32.EmptyClipboard()
        encoded = text.encode("utf-16-le") + b"\x00\x00"
        h_glb = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(encoded))
        if not h_glb:
            raise RuntimeError("Failed to allocate global memory for clipboard.")
        p_glb = kernel32.GlobalLock(h_glb)
        if not p_glb:
            raise RuntimeError("Failed to lock global memory for clipboard.")
        try:
            ctypes.memmove(p_glb, encoded, len(encoded))
        finally:
            kernel32.GlobalUnlock(h_glb)
        user32.SetClipboardData(CF_UNICODETEXT, h_glb)
    finally:
        user32.CloseClipboard()


def get_clipboard() -> str:
    """
    Read and return the current plain text contents of the Windows clipboard.
    """
    try:
        text = ""
        if HAS_PYPERCLIP:
            try:
                text = pyperclip.paste()
            except Exception as pe:
                logger.warning("pyperclip.paste failed (%s), trying Win32 ctypes fallback", pe)
                text = _get_clipboard_win32()
        else:
            text = _get_clipboard_win32()

        if not text:
            return "[Clipboard is empty or contains non-text data]"
        return text
    except Exception as e:
        logger.error("Error reading clipboard: %s", e)
        return f"Error reading clipboard contents: {str(e)}"


def set_clipboard(text: str) -> str:
    """
    Copy specified text into the active Windows system clipboard.

    Args:
        text: The string content to copy to the clipboard.
    """
    try:
        content_to_copy = str(text or "")
        if HAS_PYPERCLIP:
            try:
                pyperclip.copy(content_to_copy)
            except Exception as pe:
                logger.warning("pyperclip.copy failed (%s), trying Win32 ctypes fallback", pe)
                _set_clipboard_win32(content_to_copy)
        else:
            _set_clipboard_win32(content_to_copy)

        char_len = len(content_to_copy)
        logger.info("Copied %d characters to clipboard", char_len)
        return f"Successfully copied {char_len} character(s) to the clipboard."
    except Exception as e:
        logger.error("Error writing to clipboard: %s", e)
        return f"Error writing text to clipboard: {str(e)}"
