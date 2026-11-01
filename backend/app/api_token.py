"""Authentication for the desktop port (PLAN 4.29 A1).

Port 8000 used to answer any local process. The desktop app's ``BackendHost`` now makes a random
token per launch, hands it to uvicorn in ``JARVIS_API_TOKEN`` and sends it on every request in
``X-Jarvis-Token``; this middleware refuses what lacks it, and what a web page sent from another
origin. With no token configured (tests, a hand-started dev server) it lets everything through.
The remote port has its own gate (``app/remote.py``) and is left to it.
"""
from __future__ import annotations

import hmac
import json
from typing import Any, Iterable

TOKEN_HEADER = b"x-jarvis-token"


class ApiTokenMiddleware:
    """Pure ASGI, so streams pass straight through."""

    def __init__(self, app: Any, token: str, allowed_origins: Iterable[str], remote_port: int = 0) -> None:
        self.app = app
        self.token = token.encode("utf-8")
        self.allowed_origins = frozenset(allowed_origins)
        self.remote_port = remote_port

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if not self.token or scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        server = scope.get("server") or (None, None)
        if self.remote_port and server[1] == self.remote_port:
            return await self.app(scope, receive, send)

        headers = {key: value for key, value in scope.get("headers") or []}
        origin = headers.get(b"origin")
        if origin is not None and origin.decode("latin-1") not in self.allowed_origins:
            return await _refuse(scope, send, 403, "Foreign origin.")
        # A CORS pre-flight from an allowed origin carries no custom header by design.
        if scope.get("method") == "OPTIONS" and origin is not None:
            return await self.app(scope, receive, send)
        supplied = headers.get(TOKEN_HEADER, b"")
        if not hmac.compare_digest(supplied, self.token):
            return await _refuse(scope, send, 401, "Missing or wrong API token.")
        return await self.app(scope, receive, send)


async def _refuse(scope: dict[str, Any], send: Any, status: int, detail: str) -> None:
    if scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 1008})
        return
    body = json.dumps({"detail": detail}).encode("utf-8")
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})
