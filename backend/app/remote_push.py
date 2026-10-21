"""Notifications on the phone and iPad (PLAN 4.19 phase 2): what Jarvis says unprompted, as a web push.

Everything unprompted already goes through ``AwarenessMonitor.emit``; ``RemotePusher`` listens there
(``monitor.add_listener``, which -- unlike a stream subscriber -- doesn't count as "the app is open")
and sends the ones worth a buzz to every device that turned notifications on:

- **What:** reminders and routines (``routine:``), calendar nudges (``calendar:``), the day briefing
  (``briefing:``), an approval that timed out, and anything at warning or worse (low battery, disk,
  heat). Recoveries, "Jarvis acted" and notices stay on the desktop.
- **When:** only while the user is away from the PC -- no keyboard or mouse input for
  ``AWAY_AFTER_SECONDS`` (or the session is locked) -- so a toast and a buzz never arrive together.

**Keys.** A VAPID key pair is made on first use and kept in ``data/remote_vapid.pem``
(``JARVIS_REMOTE_VAPID_PATH`` overrides it); the phone subscribes with its public half. Each device's
subscription (the push service's endpoint and the phone's encryption keys) sits on its record in
``data/remote.json``. The payload is encrypted for the phone (RFC 8291): the push service (Google's
for Chrome, Apple's for the iPad) carries it but can't read it. A subscription the service reports
gone (404/410) is dropped.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from app.config import BASE_DIR

logger = logging.getLogger("jarvis.remote_push")

AWAY_AFTER_SECONDS = 120.0
_PUSH_PREFIXES = ("routine:", "calendar:", "briefing:")
_PUSH_KINDS = {"confirmation_timeout"}
_PUSH_MIN_RANK = 2  # Severity.WARNING
_TTL_SECONDS = 4 * 3600  # a reminder still worth showing when the phone comes back online
# The JWT's "sub" contact. Apple's push service refuses one it considers fake with 403 BadJwtToken --
# "mailto:...@jarvis.invalid" was refused on device (2026-10-21); this PC's own https tailnet address is
# accepted. Read once from Tailscale; the fallback only matters without Tailscale, when no phone can
# have subscribed anyway.
_VAPID_SUB_FALLBACK = "https://jarvis.localhost"
_vapid_sub: Optional[str] = None


def vapid_sub() -> str:
    global _vapid_sub
    if _vapid_sub is None:
        try:
            from app.routers.remote import tailscale_state

            host = tailscale_state().get("host")
        except Exception as exc:
            logger.info("Couldn't read the tailnet name for the VAPID contact: %s", exc)
            host = None
        _vapid_sub = f"https://{host}" if host else _VAPID_SUB_FALLBACK
    return _vapid_sub


def _vapid_path() -> Path:
    return Path(os.environ.get("JARVIS_REMOTE_VAPID_PATH") or BASE_DIR.parent / "data" / "remote_vapid.pem")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


_vapid_lock = threading.Lock()
_vapid: Any = None


def vapid() -> Any:
    """The server's VAPID key (py_vapid), made and saved on first use."""
    global _vapid
    with _vapid_lock:
        if _vapid is None:
            # py_vapid's Vapid is RFC 8292 ("Authorization: vapid t=..., k=..."), which Apple's push
            # service requires; Vapid01 signs the older draft (WebPush + Crypto-Key).
            from py_vapid import Vapid

            path = _vapid_path()
            if path.is_file():
                _vapid = Vapid.from_file(str(path))
            else:
                key = Vapid()
                key.generate_keys()
                path.parent.mkdir(parents=True, exist_ok=True)
                key.save_key(str(path))
                _vapid = key
                logger.info("Made the VAPID key for phone notifications: %s", path)
        return _vapid


def public_key() -> str:
    """The applicationServerKey the phone subscribes with (base64url, uncompressed P-256 point)."""
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    return _b64url(vapid().public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint))


def reset_vapid_for_tests() -> None:
    global _vapid
    with _vapid_lock:
        _vapid = None


def user_idle_seconds() -> float:
    """Seconds since the last keyboard or mouse input on this PC (GetLastInputInfo); a locked session
    counts as away. Off Windows (tests on another OS): always away."""
    if sys.platform != "win32":
        return float("inf")
    import ctypes
    from ctypes import wintypes

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    info = LASTINPUTINFO(cbSize=ctypes.sizeof(LASTINPUTINFO))
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    if _session_locked():
        return float("inf")
    return max(0.0, (ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000.0)


def _session_locked() -> bool:
    import ctypes

    # The input desktop isn't the user's while the lock screen is up: opening it fails.
    desk = ctypes.windll.user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP
    if not desk:
        return True
    ctypes.windll.user32.CloseDesktop(desk)
    return False


def worth_pushing(payload: dict[str, Any]) -> bool:
    kind = str(payload.get("kind") or "")
    if payload.get("resolved") or kind.endswith("_action"):
        return False
    if kind.startswith(_PUSH_PREFIXES) or kind in _PUSH_KINDS:
        return True
    rank = {"info": 0, "notice": 1, "warning": 2, "critical": 3}.get(str(payload.get("severity")), 0)
    return rank >= _PUSH_MIN_RANK


def notification_for(payload: dict[str, Any]) -> dict[str, Any]:
    """What the phone's service worker shows: the observation's title and its words."""
    body = payload.get("detail") or payload.get("spoken") or ""
    body = str(body).replace("{address}", "")
    return {
        "title": str(payload.get("title") or "Jarvis")[:120],
        "body": body[:400],
        "tag": str(payload.get("kind") or payload.get("id") or "jarvis"),
        "id": payload.get("id"),
        "severity": payload.get("severity"),
    }


def send(subscription: dict[str, Any], message: dict[str, Any]) -> Optional[int]:
    """One push. Returns the push service's HTTP status (201 sent; 404/410 the subscription is gone),
    or None when it couldn't be reached."""
    from pywebpush import WebPushException, webpush

    try:
        response = webpush(
            subscription_info=subscription,
            data=json.dumps(message),
            vapid_private_key=vapid(),
            vapid_claims={"sub": vapid_sub()},
            ttl=_TTL_SECONDS,
            headers={"Urgency": "high" if message.get("severity") in ("warning", "critical") else "normal"},
            timeout=10,
        )
        return getattr(response, "status_code", 201)
    except WebPushException as exc:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        # The body says why (Apple: {"reason": "BadJwtToken"}); the exception text doesn't.
        logger.info("Push refused (%s): %s", status, (getattr(response, "text", "") or str(exc))[:300])
        return status
    except Exception as exc:  # offline, DNS, TLS: try again with the next one
        logger.info("Push failed: %s", exc)
        return None


class RemotePusher:
    """The monitor's listener: picks what is worth a buzz and sends it off the event loop."""

    def __init__(self, devices_getter: Callable[[], Any], idle: Optional[Callable[[], float]] = None,
                 sender: Optional[Callable[[dict, dict], Optional[int]]] = None) -> None:
        self._devices = devices_getter
        # Looked up when used, so tests can swap the module's functions.
        self._idle = idle or (lambda: user_idle_seconds())
        self._send = sender or (lambda sub, msg: send(sub, msg))

    def __call__(self, payload: dict[str, Any]) -> None:
        try:
            if not worth_pushing(payload):
                return
            targets = self._devices().push_targets()
            if not targets or self._idle() < AWAY_AFTER_SECONDS:
                return
            message = notification_for(payload)
            threading.Thread(target=self._deliver, args=(targets, message), daemon=True,
                             name="jarvis-push").start()
        except Exception as exc:  # a notification must never break the monitor
            logger.warning("Push listener failed: %s", exc)

    def _deliver(self, targets: list[tuple[str, dict]], message: dict[str, Any]) -> None:
        for device_id, subscription in targets:
            status = self._send(subscription, message)
            if status in (404, 410):
                logger.info("Push subscription for %s is gone; dropping it.", device_id)
                self._devices().set_push(device_id, None)

    def test(self, device_id: str) -> Optional[int]:
        """Settings' "Send a test": straight to one device, away or not."""
        sub = dict(self._devices().push_targets()).get(device_id)
        if sub is None:
            return None
        return self._send(sub, {"title": "Jarvis", "body": "Notifications from your PC work.", "tag": "test"})
