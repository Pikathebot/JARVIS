import asyncio
import ctypes
import logging
import threading
from typing import Literal, Optional

logger = logging.getLogger("jarvis.agent.tools.media_control")

VK_VOLUME_MUTE = 0xAD
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF
VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_STOP = 0xB2
VK_MEDIA_PLAY_PAUSE = 0xB3
KEYEVENTF_KEYUP = 0x0002


def _send_virtual_key(vk_code: int) -> None:
    """Simulate key press and release for Windows virtual key code."""
    ctypes.windll.user32.keybd_event(vk_code, 0, 0, 0)
    ctypes.windll.user32.keybd_event(vk_code, 0, KEYEVENTF_KEYUP, 0)


_com_thread = threading.local()


def _ensure_com() -> None:
    """COM must be initialised on every thread that uses it, and tools run on a pool of worker
    threads: a call landing on a fresh one failed with "CoInitialize has not been called"
    (seen on device 2026-10-01, intermittently). Initialised once per thread and left so --
    uninitialising while pycaw's pointers are alive would crash on their release."""
    if getattr(_com_thread, "ready", False):
        return
    import comtypes
    try:
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
    except OSError:
        pass  # already initialised on this thread (in another mode): COM is usable
    _com_thread.ready = True


def _get_endpoint_volume():
    """Helper to initialize and return pycaw master audio volume endpoint."""
    _ensure_com()
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    from ctypes import cast, POINTER
    from comtypes import CLSCTX_ALL

    devices = AudioUtilities.GetSpeakers()
    if not devices:
        raise RuntimeError("No audio output speaker devices found.")
    
    # Support both pycaw legacy and modern AudioUtilities API
    if hasattr(devices, "Activate"):
        interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    elif hasattr(devices, "EndpointVolume"):
        return devices.EndpointVolume
    else:
        interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(interface, POINTER(IAudioEndpointVolume))


def set_volume(level: Optional[int] = None, change: Optional[int] = None) -> str:
    """
    Set the Windows master volume to a level (0-100), or change it by a step (change=+10 / -10); the result says old -> new. Steps by wording: "slightly"/"a tiny bit" 5, "a bit"/"louder"/"quieter" 10, "a lot"/"way louder" 20, a number the user says exactly.

    Args:
        level: Absolute master volume percentage, 0-100.
        change: Relative step in percentage points, e.g. 10 or -5.
    """
    if level is None and change is None:
        return "Error: Give a level (0-100) or a change (e.g. +10 or -10)."
    try:
        volume = _get_endpoint_volume()
        before = round(volume.GetMasterVolumeLevelScalar() * 100)
        target = int(level) if level is not None else before + int(change)
        target = max(0, min(100, target))
        volume.SetMasterVolumeLevelScalar(target / 100.0, None)
        muted = bool(volume.GetMute())
        logger.info("Volume %d%% -> %d%%", before, target)
        note = " (sound is muted, so nothing is audible until it is unmuted)" if muted else ""
        return f"Volume {before}% -> {target}%{note}."
    except Exception as e:
        logger.error("Error in set_volume: %s", e)
        return f"Error setting volume: {str(e)}"


def set_mute(on: bool) -> str:
    """
    Mute (on=true) or unmute (on=false) the Windows master audio; the result says the state now.

    Args:
        on: True to mute, False to unmute.
    """
    try:
        volume = _get_endpoint_volume()
        was = bool(volume.GetMute())
        want = bool(on) if not isinstance(on, str) else on.strip().lower() in ("true", "1", "yes", "on")
        if was != want:
            volume.SetMute(want, None)
        now = bool(volume.GetMute())
        state = "muted" if now else "unmuted"
        logger.info("Mute: %s -> %s", was, now)
        return f"Sound is {state}" + (" (it already was)." if was == now and was == want else ".")
    except Exception as e:
        logger.error("Error in set_mute: %s", e)
        return f"Error changing mute: {str(e)}"


MEDIA_ACTIONS = ("status", "play", "pause", "play_pause", "next", "previous", "stop")

_KEY_FALLBACK = {
    "play": VK_MEDIA_PLAY_PAUSE, "pause": VK_MEDIA_PLAY_PAUSE, "play_pause": VK_MEDIA_PLAY_PAUSE,
    "next": VK_MEDIA_NEXT_TRACK, "previous": VK_MEDIA_PREV_TRACK, "stop": VK_MEDIA_STOP,
}


def _app_name(source_id: str) -> str:
    """"SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify" -> "Spotify"; "chrome.exe" -> "chrome"."""
    name = (source_id or "").split("!")[-1]
    return name[:-4] if name.lower().endswith(".exe") else (name or "an app")


async def _session_action(action: str) -> str:
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as Manager,
        GlobalSystemMediaTransportControlsSessionPlaybackStatus as Status,
    )

    manager = await Manager.request_async()
    session = manager.get_current_session()
    if session is None:
        if action == "status":
            return "Nothing is playing (no app has a media session open)."
        return f"Nothing to {action.replace('_', '/')}: no app has a media session open."

    async def describe() -> str:
        props = await session.try_get_media_properties_async()
        status = session.get_playback_info().playback_status
        state = {Status.PLAYING: "Playing", Status.PAUSED: "Paused", Status.STOPPED: "Stopped"}.get(status, "Idle")
        what = " -- ".join(x for x in ((props.title or "").strip(), (props.artist or "").strip()) if x) if props else ""
        return f"{state} in {_app_name(session.source_app_user_model_id)}" + (f": {what}" if what else "") + "."

    if action == "status":
        return await describe()
    calls = {
        "play": session.try_play_async, "pause": session.try_pause_async,
        "play_pause": session.try_toggle_play_pause_async, "next": session.try_skip_next_async,
        "previous": session.try_skip_previous_async, "stop": session.try_stop_async,
    }
    ok = await calls[action]()
    if not ok:
        return f"{_app_name(session.source_app_user_model_id)} didn't accept '{action}'. " + await describe()
    await asyncio.sleep(0.4)  # the app updates its state a moment later
    return await describe()


def media_control(action: str = "status") -> str:
    """
    Control or check what is playing (Spotify, a browser video, ...): action = status ("what's playing"), play, pause, play_pause, next, previous or stop; the result says what is playing now, or that nothing is.

    Args:
        action: status, play, pause, play_pause, next, previous or stop.
    """
    act = str(action or "status").strip().lower().replace("-", "_").replace(" ", "_")
    act = {"prev": "previous", "skip": "next", "resume": "play", "toggle": "play_pause"}.get(act, act)
    if act not in MEDIA_ACTIONS:
        return f"Error: Unknown media action '{action}'. Valid actions are: {', '.join(MEDIA_ACTIONS)}."
    try:
        return asyncio.run(_session_action(act))
    except ImportError:
        logger.info("WinRT media packages missing; falling back to media keys")
    except Exception as e:
        logger.warning("Media session call failed (%s); falling back to media keys", e)
    if act == "status":
        return "Can't tell what is playing right now (the Windows media API isn't available)."
    _send_virtual_key(_KEY_FALLBACK[act])
    return f"Sent the '{act}' media key (couldn't check what is playing)."
