"""Jarvis from the phone and iPad (PLAN 4.19): who is remote, which devices are paired, and what a
remote request may reach.

**Remote is decided by the server, never by the client.** The backend listens on a second loopback
port (``REMOTE_PORT``, 8005) that nothing but ``tailscale serve`` forwards to; a request that arrived
on that port is remote, whatever its headers say. The desktop app keeps talking to :8000, which this
middleware leaves alone.

**Pairing.** Desktop Settings asks for a pairing code (``new_pairing_code``: six characters, ten
minutes, one at a time) and shows it with a QR of ``https://<pc>/m/#pair=<code>``. The phone trades
the code for a device token (``pair``); only the token's SHA-256 is kept, in ``data/remote.json``
(``JARVIS_REMOTE_DEVICES_PATH`` overrides it). Every remote request but the app's own files and the
pairing call carries ``Authorization: Bearer <token>``. Removing a device signs it out at once.

**Reach.** A remote request can only touch ``REMOTE_ROUTES``: the Freeform thread, its chat turns,
approvals and the status / Game mode / Governor controls. Pairing management, models, files,
memories and the rest stay desktop-only, so a lost phone is a smaller loss.

A remote chat turn runs with ``remote_turn`` set, and the permission gate asks for more there
(``app.agent.permissions``): you can't see the screen from the phone.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from app.config import BASE_DIR

logger = logging.getLogger("jarvis.remote")

PAIRING_TTL_SECONDS = 600
# No 0/O, 1/I/L: the code is also typed by hand from the Settings screen.
_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
_CODE_LENGTH = 6
_LAST_USED_WRITE_EVERY = 60.0  # seconds: last_used_at is for the device list, not an audit log
_MAX_FAILED_PAIRS = 10  # per code: a wrong guess burns a try, then the code is dropped

_remote_turn: contextvars.ContextVar[bool] = contextvars.ContextVar("jarvis_remote_turn", default=False)


def remote_turn() -> bool:
    """True while a chat turn that came from a paired phone or iPad is running."""
    return _remote_turn.get()


def set_remote_turn(value: bool) -> contextvars.Token:
    return _remote_turn.set(value)


def _devices_path() -> Path:
    return Path(os.environ.get("JARVIS_REMOTE_DEVICES_PATH") or BASE_DIR.parent / "data" / "remote.json")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def normalise_code(code: str) -> str:
    return re.sub(r"[^0-9A-Z]", "", (code or "").upper())


class RemoteDevices:
    """The paired devices (``data/remote.json``) and the one live pairing code (memory only)."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or _devices_path()
        self._lock = threading.Lock()
        self._devices: list[dict[str, Any]] = []
        self._code: Optional[str] = None
        self._code_expires = 0.0
        self._code_failures = 0
        self._last_write: dict[str, float] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._devices = [d for d in data.get("devices", []) if d.get("token_sha256")]
        except FileNotFoundError:
            self._devices = []
        except Exception as exc:  # a broken file pairs nothing rather than crash the backend
            logger.warning("Could not read %s (%s); no devices paired.", self._path, exc)
            self._devices = []

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"devices": self._devices}, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)

    # ---- pairing code -------------------------------------------------------------------------

    def new_pairing_code(self) -> dict[str, Any]:
        with self._lock:
            self._code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))
            self._code_expires = time.time() + PAIRING_TTL_SECONDS
            self._code_failures = 0
            return {"code": self._code, "expires_at": self._code_expires, "ttl_seconds": PAIRING_TTL_SECONDS}

    def current_code(self) -> Optional[dict[str, Any]]:
        with self._lock:
            if self._code and time.time() < self._code_expires:
                return {"code": self._code, "expires_at": self._code_expires,
                        "ttl_seconds": max(0, int(self._code_expires - time.time()))}
            return None

    def pair(self, code: str, name: str) -> Optional[dict[str, Any]]:
        """Trade a live pairing code for a device token (returned once, never stored). The code is
        used up. None when the code is wrong or expired."""
        with self._lock:
            live = self._code is not None and time.time() < self._code_expires
            if not live or not secrets.compare_digest(normalise_code(code), self._code or ""):
                if live:
                    self._code_failures += 1
                    if self._code_failures >= _MAX_FAILED_PAIRS:
                        self._code = None
                return None
            self._code = None
            token = secrets.token_urlsafe(32)
            device = {
                "id": uuid.uuid4().hex[:12],
                "name": (name or "").strip()[:60] or "Phone",
                "token_sha256": _hash(token),
                "created_at": time.time(),
                "last_used_at": time.time(),
            }
            self._devices.append(device)
            self._save()
            logger.info("Paired a remote device: %s (%s).", device["name"], device["id"])
            return {"token": token, "device": self.public(device)}

    # ---- devices ------------------------------------------------------------------------------

    @staticmethod
    def public(device: dict[str, Any]) -> dict[str, Any]:
        """The record without its secrets: no token hash, and only whether notifications are on."""
        out = {k: v for k, v in device.items() if k not in ("token_sha256", "push")}
        out["notifications"] = bool(device.get("push"))
        return out

    def set_push(self, device_id: str, subscription: Optional[dict[str, Any]]) -> bool:
        """A device's web-push subscription (app/remote_push.py); None turns its notifications off."""
        with self._lock:
            for device in self._devices:
                if device["id"] == device_id:
                    if subscription:
                        device["push"] = subscription
                    else:
                        device.pop("push", None)
                    self._save()
                    return True
            return False

    def push_targets(self) -> list[tuple[str, dict[str, Any]]]:
        with self._lock:
            return [(d["id"], d["push"]) for d in self._devices if d.get("push")]

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self.public(d) for d in self._devices]

    def authenticate(self, token: str) -> Optional[dict[str, Any]]:
        if not token:
            return None
        digest = _hash(token)
        with self._lock:
            for device in self._devices:
                if secrets.compare_digest(device["token_sha256"], digest):
                    now = time.time()
                    device["last_used_at"] = now
                    if now - self._last_write.get(device["id"], 0.0) >= _LAST_USED_WRITE_EVERY:
                        self._last_write[device["id"]] = now
                        self._save()
                    return self.public(device)
        return None

    def remove(self, device_id: str) -> bool:
        with self._lock:
            kept = [d for d in self._devices if d["id"] != device_id]
            if len(kept) == len(self._devices):
                return False
            self._devices = kept
            self._save()
            return True


_registry: Optional[RemoteDevices] = None


def get_remote_devices() -> RemoteDevices:
    global _registry
    if _registry is None:
        _registry = RemoteDevices()
    return _registry


def reset_remote_devices_for_tests(path: Optional[Path] = None) -> RemoteDevices:
    global _registry
    _registry = RemoteDevices(path)
    return _registry


# ---- what a remote request may reach -----------------------------------------------------------

# (method, path regex). Anything else from the remote port is a 404, as if it weren't there.
REMOTE_ROUTES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (m, re.compile(p)) for m, p in (
        ("POST", r"/chat/stream"),
        ("GET", r"/api/thread/(episodes|messages|head|live)"),
        ("GET", r"/api/confirmations"),
        ("POST", r"/api/confirmations/act_[0-9a-f]+/deny"),
        ("GET", r"/governor/status"),
        ("POST", r"/governor/(auto|resume)"),
        ("GET", r"/api/game-mode"),
        ("POST", r"/api/game-mode"),
        ("GET", r"/health"),
        ("GET", r"/api/remote/me"),
        ("GET", r"/api/remote/status"),
        ("GET", r"/api/remote/push/key"),
        ("POST", r"/api/remote/push(/test)?"),
        ("DELETE", r"/api/remote/push"),
        ("POST", r"/api/remote/(transcribe|say)"),  # the phone's mic and spoken replies (phase 3)
        ("POST", r"/models/warm"),  # load the chat model ahead of a message (main only, no arguments)
    )
)
# Reachable without a token: the app itself (so an unpaired phone can show the pairing screen)
# and the pairing call.
_OPEN_PREFIXES = ("/m/",)
_OPEN_EXACT = {("GET", "/m"), ("POST", "/api/remote/pair")}


def remote_route_allowed(method: str, path: str) -> bool:
    return any(m == method and rx.fullmatch(path) for m, rx in REMOTE_ROUTES)


def _bearer(scope: dict[str, Any]) -> str:
    for key, value in scope.get("headers") or []:
        if key == b"authorization":
            text = value.decode("latin-1")
            if text[:7].lower() == "bearer ":
                return text[7:].strip()
    return ""


class RemoteGateMiddleware:
    """Pure ASGI (streams pass straight through). Marks requests that came in on the remote port
    (``scope["state"]["remote"]``, read with ``is_remote_request``), and refuses what a remote
    caller may not reach or has no valid device token for. The desktop's port is untouched."""

    def __init__(self, app: Any, remote_port: int) -> None:
        self.app = app
        self.remote_port = remote_port

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        server = scope.get("server") or (None, None)
        if not self.remote_port or server[1] != self.remote_port:
            return await self.app(scope, receive, send)

        state = scope.setdefault("state", {})
        state["remote"] = True
        method, path = scope.get("method", "GET"), scope.get("path", "")
        if scope["type"] == "websocket":
            return await _refuse(send, 404, "Not available remotely.")
        if method == "OPTIONS":  # same origin through serve; nothing to pre-flight
            return await _refuse(send, 404, "Not available remotely.")
        if path.startswith(_OPEN_PREFIXES) or (method, path) in _OPEN_EXACT:
            return await self.app(scope, receive, send)
        if not remote_route_allowed(method, path):
            return await _refuse(send, 404, "Not available remotely.")
        device = get_remote_devices().authenticate(_bearer(scope))
        if device is None:
            return await _refuse(send, 401, "This device isn't paired with Jarvis.")
        state["remote_device"] = device
        return await self.app(scope, receive, send)


async def _refuse(send: Any, status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode("utf-8")
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


def is_remote_request(request: Any) -> bool:
    """The middleware's verdict for a Starlette ``Request``."""
    return bool(getattr(request, "scope", {}).get("state", {}).get("remote"))


def remote_device(request: Any) -> Optional[dict[str, Any]]:
    return getattr(request, "scope", {}).get("state", {}).get("remote_device")
