"""Google sign-in for Jarvis (PLAN 4.16 phase B): the OAuth "desktop app" flow, by hand over httpx.

The user's own Google Cloud client (docs/GOOGLE_SETUP.md) lives in repo-root
``data/google_client_secret.json``. Signing in opens the browser once; Google redirects to a
one-shot loopback server on 127.0.0.1 with a code, exchanged (with PKCE) for a refresh token.
The token is stored in ``data/google_token.bin`` encrypted with Windows DPAPI, so only this
Windows account on this PC can read it. No google-auth library: the flow is three HTTP calls.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from app.config import BASE_DIR

logger = logging.getLogger("jarvis.google.auth")

DATA_DIR = Path(os.environ.get("JARVIS_GOOGLE_DIR") or BASE_DIR.parent / "data")  # tests: a temp dir
CLIENT_SECRET_PATH = DATA_DIR / "google_client_secret.json"
TOKEN_PATH = DATA_DIR / "google_token.bin"
SCOPES = ("https://www.googleapis.com/auth/calendar.events", "https://www.googleapis.com/auth/tasks")
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
SIGN_IN_TIMEOUT_S = 300


class GoogleNotConnected(Exception):
    """Google isn't set up (no client file) or not signed in; the message says which, for the user."""


# --- seams the tests replace ------------------------------------------------------------------

def _http() -> httpx.Client:
    return httpx.Client(timeout=20)


def _protect(data: bytes) -> bytes:
    import win32crypt  # pywin32; DPAPI, scoped to the current Windows user

    return win32crypt.CryptProtectData(data, "Jarvis Google sign-in", None, None, None, 0)


def _unprotect(data: bytes) -> bytes:
    import win32crypt

    return win32crypt.CryptUnprotectData(data, None, None, None, 0)[1]


def _open_browser(url: str) -> None:
    import webbrowser

    webbrowser.open(url)


# --- client and token files -------------------------------------------------------------------

def load_client() -> tuple[str, str]:
    """(client_id, client_secret) from the user's downloaded client JSON."""
    if not CLIENT_SECRET_PATH.is_file():
        raise GoogleNotConnected("Google isn't set up yet: the client file data/google_client_secret.json is "
                                 "missing (see docs/GOOGLE_SETUP.md).")
    try:
        raw = json.loads(CLIENT_SECRET_PATH.read_text(encoding="utf-8"))
        block = raw.get("installed") or raw.get("web") or {}
        return block["client_id"], block["client_secret"]
    except (ValueError, KeyError, AttributeError) as exc:
        raise GoogleNotConnected(f"data/google_client_secret.json isn't a Google OAuth client file ({exc}).")


_lock = threading.Lock()


def _load_token() -> Optional[dict[str, Any]]:
    if not TOKEN_PATH.is_file():
        return None
    try:
        return json.loads(_unprotect(TOKEN_PATH.read_bytes()).decode("utf-8"))
    except Exception as exc:  # another Windows user, a corrupt file: treat as signed out
        logger.warning("Google token unreadable, treating as signed out: %s", exc)
        return None


def _save_token(token: dict[str, Any]) -> None:
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = TOKEN_PATH.with_suffix(".tmp")
    tmp.write_bytes(_protect(json.dumps(token).encode("utf-8")))
    tmp.replace(TOKEN_PATH)


def _with_expiry(resp: dict[str, Any], old: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    token = dict(old or {})
    token["access_token"] = resp["access_token"]
    token["expires_at"] = time.time() + float(resp.get("expires_in", 3600))
    if resp.get("refresh_token"):  # Google sends it on the first exchange only
        token["refresh_token"] = resp["refresh_token"]
    if resp.get("scope"):
        token["scope"] = resp["scope"]
    return token


def access_token() -> str:
    """A valid access token, refreshed when it has under a minute left. Raises GoogleNotConnected."""
    with _lock:
        token = _load_token()
        if not token or not token.get("refresh_token"):
            raise GoogleNotConnected("Not signed in to Google yet.")
        if token.get("access_token") and float(token.get("expires_at", 0)) - time.time() > 60:
            return token["access_token"]
        client_id, client_secret = load_client()
        with _http() as http:
            r = http.post(TOKEN_URL, data={"client_id": client_id, "client_secret": client_secret,
                                           "refresh_token": token["refresh_token"], "grant_type": "refresh_token"})
        if r.status_code in (400, 401):  # revoked, or expired (an app left in "Testing" expires in 7 days)
            raise GoogleNotConnected("The Google sign-in has expired or was revoked; sign in again.")
        r.raise_for_status()
        token = _with_expiry(r.json(), token)
        _save_token(token)
        return token["access_token"]


# --- signing in -------------------------------------------------------------------------------

class _SignIn:
    def __init__(self) -> None:
        self.server: Optional[HTTPServer] = None
        self.state = ""
        self.error = ""
        self.done = threading.Event()


_pending: Optional[_SignIn] = None


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def start_sign_in(open_browser: Callable[[str], None] = None) -> str:
    """Start the browser sign-in; returns the URL it opened. The loopback server waits up to five
    minutes for Google's redirect, exchanges the code and stores the token."""
    global _pending
    client_id, client_secret = load_client()
    if _pending is not None and not _pending.done.is_set():
        _finish(_pending, "superseded by a new sign-in")
    flow = _SignIn()
    flow.state = secrets.token_urlsafe(24)
    verifier, challenge = _pkce()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (http.server's name)
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" not in q and "error" not in q:
                self.send_response(404)
                self.end_headers()
                return
            ok, msg = False, ""
            if q.get("state", [""])[0] != flow.state:
                msg = "the sign-in didn't match (state); try again from Jarvis"
            elif "error" in q:
                msg = q["error"][0]
            else:
                try:
                    with _http() as http:
                        r = http.post(TOKEN_URL, data={
                            "client_id": client_id, "client_secret": client_secret, "code": q["code"][0],
                            "code_verifier": verifier, "grant_type": "authorization_code", "redirect_uri": redirect})
                    r.raise_for_status()
                    with _lock:
                        _save_token(_with_expiry(r.json()))
                    ok = True
                except Exception as exc:
                    msg = f"the token exchange failed ({exc})"
            body = ("Jarvis is connected to Google. You can close this tab." if ok else
                    f"Jarvis couldn't connect to Google: {msg}.").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
            _finish(flow, "" if ok else msg)

        def log_message(self, *args: Any) -> None:
            pass

    flow.server = HTTPServer(("127.0.0.1", 0), Handler)
    redirect = f"http://127.0.0.1:{flow.server.server_address[1]}"
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code", "scope": " ".join(SCOPES),
        "state": flow.state, "code_challenge": challenge, "code_challenge_method": "S256",
        "access_type": "offline", "prompt": "consent"})
    _pending = flow
    threading.Thread(target=_serve, args=(flow,), name="google-sign-in", daemon=True).start()
    (open_browser or _open_browser)(url)
    return url


def _serve(flow: _SignIn) -> None:
    server = flow.server
    server.timeout = 1
    deadline = time.time() + SIGN_IN_TIMEOUT_S
    while not flow.done.is_set() and time.time() < deadline:
        server.handle_request()
    if not flow.done.is_set():
        _finish(flow, "timed out waiting for the browser")
    server.server_close()


def _finish(flow: _SignIn, error: str) -> None:
    flow.error = error
    flow.done.set()
    if error:
        logger.info("Google sign-in ended: %s", error)
    else:
        logger.info("Google sign-in complete")


def sign_out() -> bool:
    """Revoke the token at Google (best effort) and delete it here; False if there was none."""
    with _lock:
        token = _load_token()
        TOKEN_PATH.unlink(missing_ok=True)
    if not token:
        return False
    try:
        with _http() as http:
            http.post(REVOKE_URL, data={"token": token.get("refresh_token") or token.get("access_token", "")})
    except Exception as exc:
        logger.info("Google revoke failed (token deleted here anyway): %s", exc)
    return True


def status() -> dict[str, Any]:
    token = _load_token()
    pending = _pending is not None and not _pending.done.is_set()
    return {
        "configured": CLIENT_SECRET_PATH.is_file(),
        "signed_in": bool(token and token.get("refresh_token")),
        "scopes": (token or {}).get("scope", "").split(),
        "signing_in": pending,
        "last_error": (_pending.error if _pending is not None and not pending else ""),
    }
