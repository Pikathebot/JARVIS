"""PLAN 4.16 phase B: Google sign-in (OAuth desktop flow with PKCE, DPAPI-stored token)."""
import json
import threading
import time
import urllib.parse
import urllib.request

import httpx
import pytest

from app.google import auth


@pytest.fixture
def google(tmp_path, monkeypatch):
    """Client and token files in a temp dir, a reversible fake DPAPI, and a fake Google."""
    secret = tmp_path / "google_client_secret.json"
    secret.write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "csecret"}}), encoding="utf-8")
    monkeypatch.setattr(auth, "CLIENT_SECRET_PATH", secret)
    monkeypatch.setattr(auth, "TOKEN_PATH", tmp_path / "google_token.bin")
    monkeypatch.setattr(auth, "_protect", lambda b: b"P" + b[::-1])
    monkeypatch.setattr(auth, "_unprotect", lambda b: b[1:][::-1])
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        form = dict(urllib.parse.parse_qsl(req.content.decode()))
        calls.append((str(req.url), form))
        if str(req.url) == auth.TOKEN_URL and form.get("grant_type") == "authorization_code":
            return httpx.Response(200, json={"access_token": "a1", "expires_in": 3600, "refresh_token": "r1",
                                             "scope": " ".join(auth.SCOPES)})
        if str(req.url) == auth.TOKEN_URL and form.get("grant_type") == "refresh_token":
            if form.get("refresh_token") == "revoked":
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "a2", "expires_in": 3600})
        return httpx.Response(200, json={})

    monkeypatch.setattr(auth, "_http", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(auth, "_pending", None)
    return calls


def _browser_returns(params_from_url):
    """A fake browser: reads the auth URL, then hits the loopback redirect like Google would."""
    def open_browser(url):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        params = params_from_url(q)

        def go():
            urllib.request.urlopen(q["redirect_uri"] + "/?" + urllib.parse.urlencode(params), timeout=5).read()
        threading.Thread(target=go, daemon=True).start()
    return open_browser


def _wait_signed_in():
    for _ in range(100):
        if not auth.status()["signing_in"]:
            return
        time.sleep(0.05)


def test_not_set_up_and_not_signed_in_are_said_plainly(google, tmp_path, monkeypatch):
    assert auth.status() == {"configured": True, "signed_in": False, "scopes": [], "signing_in": False,
                             "last_error": ""}
    with pytest.raises(auth.GoogleNotConnected, match="Not signed in"):
        auth.access_token()
    monkeypatch.setattr(auth, "CLIENT_SECRET_PATH", tmp_path / "missing.json")
    with pytest.raises(auth.GoogleNotConnected, match="GOOGLE_SETUP"):
        auth.start_sign_in(open_browser=lambda url: None)


def test_sign_in_exchanges_the_code_with_pkce_and_stores_the_token_encrypted(google):
    seen = {}

    def params(q):
        seen.update(q)
        return {"code": "c1", "state": q["state"]}

    url = auth.start_sign_in(open_browser=_browser_returns(params))
    _wait_signed_in()
    assert url.startswith(auth.AUTH_URL) and seen["code_challenge_method"] == "S256"
    assert seen["access_type"] == "offline" and set(seen["scope"].split()) == set(auth.SCOPES)
    assert seen["redirect_uri"].startswith("http://127.0.0.1:")
    exchange = [f for u, f in google if f.get("grant_type") == "authorization_code"][0]
    assert exchange["code"] == "c1" and len(exchange["code_verifier"]) >= 43
    assert exchange["redirect_uri"] == seen["redirect_uri"]
    assert b"r1" not in auth.TOKEN_PATH.read_bytes()[:1]  # stored through _protect, not as plain JSON
    st = auth.status()
    assert st["signed_in"] and not st["signing_in"] and st["last_error"] == ""
    assert auth.access_token() == "a1"


def test_a_wrong_state_is_refused(google):
    auth.start_sign_in(open_browser=_browser_returns(lambda q: {"code": "c1", "state": "forged"}))
    _wait_signed_in()
    st = auth.status()
    assert not st["signed_in"] and "state" in st["last_error"]
    assert not [f for u, f in google if f.get("grant_type") == "authorization_code"]


def test_an_expiring_token_is_refreshed_and_a_revoked_one_says_sign_in_again(google):
    auth._save_token({"access_token": "old", "expires_at": time.time() + 10, "refresh_token": "r1"})
    assert auth.access_token() == "a2"
    assert auth.access_token() == "a2"  # fresh now: no second refresh
    assert len([f for u, f in google if f.get("grant_type") == "refresh_token"]) == 1
    auth._save_token({"access_token": "old", "expires_at": 0, "refresh_token": "revoked"})
    with pytest.raises(auth.GoogleNotConnected, match="sign in again"):
        auth.access_token()


def test_sign_out_revokes_and_deletes(google):
    auth._save_token({"access_token": "a1", "expires_at": time.time() + 3600, "refresh_token": "r1"})
    assert auth.sign_out() is True
    assert not auth.TOKEN_PATH.exists()
    assert (auth.REVOKE_URL, {"token": "r1"}) in google
    assert auth.sign_out() is False


def test_the_endpoints(google):
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    assert client.get("/api/google/status").json()["configured"] is True
    assert client.post("/api/google/sign-out").json() == {"signed_out": False}
