from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api_token import ApiTokenMiddleware

ALLOWED = ["http://localhost:8000"]


def _client(token: str = "secret", remote_port: int = 0) -> TestClient:
    inner = FastAPI()

    @inner.get("/api/ping")
    def ping():
        return {"ok": True}

    inner.add_middleware(ApiTokenMiddleware, token=token, allowed_origins=ALLOWED, remote_port=remote_port)
    return TestClient(inner)  # server is ("testserver", 80)


def test_request_without_the_token_is_refused():
    assert _client().get("/api/ping").status_code == 401


def test_wrong_token_is_refused():
    assert _client().get("/api/ping", headers={"X-Jarvis-Token": "guess"}).status_code == 401


def test_right_token_passes():
    assert _client().get("/api/ping", headers={"X-Jarvis-Token": "secret"}).json() == {"ok": True}


def test_foreign_origin_is_refused_even_with_the_token():
    r = _client().get("/api/ping", headers={"X-Jarvis-Token": "secret", "Origin": "https://evil.example"})
    assert r.status_code == 403


def test_allowed_origin_with_the_token_passes():
    r = _client().get("/api/ping", headers={"X-Jarvis-Token": "secret", "Origin": ALLOWED[0]})
    assert r.status_code == 200


def test_no_token_configured_leaves_the_port_open():
    assert _client(token="").get("/api/ping").status_code == 200


def test_remote_port_is_left_to_its_own_gate():
    assert _client(remote_port=80).get("/api/ping").status_code == 200
