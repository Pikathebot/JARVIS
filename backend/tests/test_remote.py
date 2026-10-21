"""Phone/iPad remote control (PLAN 4.19 phase 1): pairing, the remote port's gate, the stricter
permission rule on remote turns, and that remote chat turns land in the Freeform thread."""
import json

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app import remote as remote_mod
from app.agent.permissions import RiskTier, evaluate_tool_permission
from app.remote import RemoteDevices, RemoteGateMiddleware, is_remote_request, remote_route_allowed


@pytest.fixture
def devices(tmp_path):
    return remote_mod.reset_remote_devices_for_tests(tmp_path / "remote.json")


# ---- pairing ----------------------------------------------------------------------------------

def test_pairing_code_buys_one_token_and_is_used_up(devices):
    code = devices.new_pairing_code()["code"]
    assert len(code) == 6 and not set(code) & set("01OIL")
    paired = devices.pair(code.lower(), "Pixel")  # typed by hand: case doesn't matter
    assert paired and paired["device"]["name"] == "Pixel"
    assert devices.authenticate(paired["token"])["id"] == paired["device"]["id"]
    assert devices.pair(code, "Again") is None  # used up


def test_only_the_token_hash_is_stored(devices, tmp_path):
    paired = devices.pair(devices.new_pairing_code()["code"], "iPad")
    text = (tmp_path / "remote.json").read_text(encoding="utf-8")
    assert paired["token"] not in text and "token_sha256" in text
    assert "token_sha256" not in json.dumps(devices.list())
    # Survives a restart.
    assert RemoteDevices(tmp_path / "remote.json").authenticate(paired["token"]) is not None


def test_wrong_and_expired_codes_fail(devices, monkeypatch):
    code = devices.new_pairing_code()["code"]
    assert devices.pair("ZZZZZZ" if code != "ZZZZZZ" else "YYYYYY", "x") is None
    monkeypatch.setattr(remote_mod.time, "time", lambda: 10**12)
    assert devices.pair(code, "x") is None


def test_repeated_wrong_guesses_drop_the_code(devices):
    code = devices.new_pairing_code()["code"]
    wrong = "ZZZZZZ" if code != "ZZZZZZ" else "YYYYYY"
    for _ in range(remote_mod._MAX_FAILED_PAIRS):
        devices.pair(wrong, "x")
    assert devices.current_code() is None and devices.pair(code, "x") is None


def test_removing_a_device_signs_it_out(devices):
    paired = devices.pair(devices.new_pairing_code()["code"], "Pixel")
    assert devices.remove(paired["device"]["id"])
    assert devices.authenticate(paired["token"]) is None
    assert not devices.remove(paired["device"]["id"])


# ---- the remote port's gate ---------------------------------------------------------------------

def _gated_app(remote_port: int) -> TestClient:
    inner = FastAPI()

    @inner.get("/api/thread/messages")
    def msgs(request: Request):
        return {"remote": is_remote_request(request)}

    @inner.get("/api/remote/devices")
    def devs():
        return {"devices": []}

    @inner.post("/api/remote/pair")
    def pair():
        return {"ok": True}

    @inner.get("/m/index.html")
    def page():
        return {"page": True}

    inner.add_middleware(RemoteGateMiddleware, remote_port=remote_port)
    return TestClient(inner)  # TestClient's server is ("testserver", 80)


def test_desktop_port_is_untouched(devices):
    client = _gated_app(remote_port=8005)
    assert client.get("/api/thread/messages").json() == {"remote": False}
    assert client.get("/api/remote/devices").status_code == 200


def test_remote_port_needs_a_paired_token(devices):
    client = _gated_app(remote_port=80)
    assert client.get("/api/thread/messages").status_code == 401
    assert client.get("/api/thread/messages", headers={"Authorization": "Bearer nope"}).status_code == 401
    token = devices.pair(devices.new_pairing_code()["code"], "Pixel")["token"]
    r = client.get("/api/thread/messages", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200 and r.json() == {"remote": True}


def test_remote_port_opens_only_the_app_and_pairing(devices):
    client = _gated_app(remote_port=80)
    assert client.get("/m/index.html").status_code == 200
    assert client.post("/api/remote/pair").status_code == 200
    token = devices.pair(devices.new_pairing_code()["code"], "Pixel")["token"]
    # Desktop-only routes are not there, even with a good token.
    assert client.get("/api/remote/devices", headers={"Authorization": f"Bearer {token}"}).status_code == 404


def test_remote_route_list():
    assert remote_route_allowed("POST", "/chat/stream")
    assert remote_route_allowed("POST", "/api/confirmations/act_0123abcd0123abcd/deny")
    assert remote_route_allowed("POST", "/governor/auto")
    for method, path in [("POST", "/chat"), ("GET", "/sessions"), ("POST", "/models/load"),
                         ("DELETE", "/api/thread/episodes/3"), ("GET", "/api/memories"),
                         ("POST", "/governor/pause"), ("GET", "/api/remote/pairing"),
                         ("GET", "/api/thread/messages/../../sessions")]:
        assert not remote_route_allowed(method, path), (method, path)


# ---- the stricter gate on remote turns -----------------------------------------------------------

@pytest.fixture
def remote_turn():
    token = remote_mod.set_remote_turn(True)
    yield
    remote_mod._remote_turn.reset(token)


def test_remote_turn_asks_before_acting_on_the_screen(remote_turn):
    d = evaluate_tool_permission("focus_app", {"name": "Spotify"})
    assert not d.allowed and d.risk_tier == RiskTier.CONFIRMATION_REQUIRED
    assert d.reason.startswith("From your phone:")
    d = evaluate_tool_permission("set_clipboard", {"text": "hi"})
    assert not d.allowed


def test_remote_turn_leaves_reads_and_harmless_controls_alone(remote_turn):
    for tool, args in [("web_search", {"query": "x"}), ("set_volume", {"level": 30}),
                       ("get_system_status", {}), ("notes", {"action": "read"})]:
        assert evaluate_tool_permission(tool, args).allowed, tool


def test_remote_turn_approval_token_still_works(remote_turn):
    d = evaluate_tool_permission("focus_app", {"name": "Spotify"})
    assert evaluate_tool_permission("focus_app", {"name": "Spotify"}, approved_action_ids=[d.action_id]).allowed


def test_remote_turn_labels_cards_that_ask_anyway(remote_turn):
    d = evaluate_tool_permission("kill_process", {"pid_or_name": "notepad.exe"})
    assert not d.allowed and d.reason.startswith("From your phone:")


def test_desktop_turn_unchanged():
    assert evaluate_tool_permission("focus_app", {"name": "Spotify"}).allowed


# ---- the pairing endpoints ------------------------------------------------------------------------

def test_pairing_endpoint_gives_code_qr_and_address(monkeypatch, devices):
    import app.main as main
    from app.routers import remote as router_mod

    monkeypatch.setattr(router_mod, "tailscale_state", lambda: {
        "installed": True, "running": True, "host": "pc.tail1.ts.net", "serving": True, "serve_command": "x"})
    client = TestClient(main.app)
    first = client.get("/api/remote/pairing").json()
    assert first["url"] == f"https://pc.tail1.ts.net/m/#pair={first['code']}"
    assert first["qr_svg"].startswith("<svg") and "#000" in first["qr_svg"]
    assert client.get("/api/remote/pairing").json()["code"] == first["code"]  # still live: same code
    fresh = client.post("/api/remote/pairing").json()["code"]
    assert fresh != first["code"]
    paired = client.post("/api/remote/pair", json={"code": fresh, "name": "Pixel"}).json()
    assert paired["device"]["name"] == "Pixel" and paired["token"]
    assert [d["name"] for d in client.get("/api/remote/devices").json()["devices"]] == ["Pixel"]
    assert client.post("/api/remote/pair", json={"code": fresh, "name": "x"}).status_code == 400
    assert client.delete(f"/api/remote/devices/{paired['device']['id']}").json() == {"removed": paired["device"]["id"]}


def test_short_model_name():
    from app.routers.remote import short_model_name

    assert short_model_name("Ornith-1.5-9B.i1-IQ3_XXS.gguf") == "Ornith 1.5 9B"
    assert short_model_name("MiniCPM5-2B-Q4_K_M.gguf") == "MiniCPM5 2B"
    assert short_model_name("Qwen3.5-9B-UD-IQ3_XXS.gguf") == "Qwen3.5 9B"
    assert short_model_name(None) is None


# ---- remote chat turns land in the thread -------------------------------------------------------

def test_remote_chat_turn_is_forced_into_the_thread(monkeypatch, devices):
    import app.main as main
    from app.memory.thread import THREAD_SESSION_ID

    seen = {}

    class FakeOrchestrator:
        def __init__(self, **kwargs):
            pass

        async def run_stream(self, **kwargs):
            seen.update(kwargs)
            seen["remote_turn"] = remote_mod.remote_turn()
            yield {"event": "done", "data": {"response": "ok"}}

    monkeypatch.setattr(main, "AgentOrchestrator", FakeOrchestrator)
    monkeypatch.setattr(main, "is_remote_request", lambda request: True)
    monkeypatch.setattr(main.settings, "governor_enabled", False)
    r = TestClient(main.app).post("/chat/stream", json={"message": "hello", "session_id": "other",
                                                        "chat_mode": "WORKSPACE", "project_id": "p1"})
    assert r.status_code == 200 and "event: done" in r.text
    assert seen["session_id"] == THREAD_SESSION_ID and seen["chat_mode"] == "FREEFORM"
    assert seen["project_id"] is None and seen["remote_turn"] is True
