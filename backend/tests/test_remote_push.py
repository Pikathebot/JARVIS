"""Phone notifications (PLAN 4.19 phase 2): what is worth a buzz, only while away from the PC,
subscriptions on the device record, and the monitor listener that isn't "the app is open"."""
import threading

import pytest
from fastapi.testclient import TestClient

from app import remote as remote_mod
from app import remote_push
from app.remote_push import RemotePusher, notification_for, worth_pushing

SUB = {"endpoint": "https://fcm.googleapis.com/fcm/send/abc", "keys": {"p256dh": "BPk", "auth": "xyz"}}


@pytest.fixture
def devices(tmp_path):
    return remote_mod.reset_remote_devices_for_tests(tmp_path / "remote.json")


def _paired(devices, name="Pixel"):
    return devices.pair(devices.new_pairing_code()["code"], name)["device"]


@pytest.mark.parametrize("payload,expected", [
    ({"kind": "routine:abc", "severity": "info"}, True),
    ({"kind": "calendar:ev1", "severity": "info"}, True),
    ({"kind": "briefing:2026-10-21", "severity": "info"}, True),
    ({"kind": "confirmation_timeout", "severity": "warning"}, True),
    ({"kind": "battery_level", "severity": "critical"}, True),
    ({"kind": "gpu_thermal", "severity": "warning"}, True),
    ({"kind": "cpu_saturation", "severity": "notice"}, False),
    ({"kind": "model_evicted", "severity": "notice"}, False),
    ({"kind": "vram_pressure", "severity": "info", "resolved": True}, False),
    ({"kind": "battery_level_action", "severity": "notice"}, False),
])
def test_worth_pushing(payload, expected):
    assert worth_pushing(payload) is expected


def test_notification_text_comes_from_the_observation():
    msg = notification_for({"kind": "routine:a", "title": "Stretch", "detail": "", "spoken": "Time to stretch{address}."})
    assert msg == {"title": "Stretch", "body": "Time to stretch.", "tag": "routine:a", "id": None, "severity": None}


def test_subscription_lives_on_the_device_and_stays_private(devices):
    device = _paired(devices)
    assert devices.set_push(device["id"], SUB)
    assert devices.push_targets() == [(device["id"], SUB)]
    listed = devices.list()[0]
    assert listed["notifications"] is True and "push" not in listed
    devices.set_push(device["id"], None)
    assert devices.push_targets() == [] and devices.list()[0]["notifications"] is False


def _pusher(devices, idle, sent):
    done = threading.Event()

    def sender(sub, msg):
        sent.append((sub, msg))
        done.set()
        return 201

    return RemotePusher(lambda: devices, idle=lambda: idle, sender=sender), done


def test_pushes_only_while_away_from_the_pc(devices):
    devices.set_push(_paired(devices)["id"], SUB)
    sent = []
    at_desk, _ = _pusher(devices, idle=5.0, sent=sent)
    at_desk({"kind": "routine:a", "severity": "info", "title": "Stretch"})
    assert sent == []
    away, done = _pusher(devices, idle=remote_push.AWAY_AFTER_SECONDS + 1, sent=sent)
    away({"kind": "cpu_saturation", "severity": "notice", "title": "Busy"})  # not worth a buzz
    away({"kind": "routine:a", "severity": "info", "title": "Stretch", "detail": "Time to stretch."})
    assert done.wait(5)
    assert sent == [(SUB, {"title": "Stretch", "body": "Time to stretch.", "tag": "routine:a", "id": None, "severity": "info"})]


def test_a_gone_subscription_is_dropped(devices):
    device = _paired(devices)
    devices.set_push(device["id"], SUB)
    pusher = RemotePusher(lambda: devices, idle=lambda: 1e9, sender=lambda sub, msg: 410)
    pusher._deliver(devices.push_targets(), {"title": "x"})
    assert devices.push_targets() == []


def test_vapid_key_is_made_once_and_kept(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_REMOTE_VAPID_PATH", str(tmp_path / "vapid.pem"))
    remote_push.reset_vapid_for_tests()
    key = remote_push.public_key()
    assert len(key) == 87 and (tmp_path / "vapid.pem").is_file()  # 65-byte P-256 point, base64url
    remote_push.reset_vapid_for_tests()
    assert remote_push.public_key() == key
    remote_push.reset_vapid_for_tests()


def test_vapid_contact_is_the_pcs_tailnet_address(monkeypatch):
    """Apple refused "mailto:...@jarvis.invalid" (403 BadJwtToken) and accepted the https tailnet name."""
    from app.routers import remote as router_mod

    monkeypatch.setattr(remote_push, "_vapid_sub", None)
    monkeypatch.setattr(router_mod, "tailscale_state", lambda: {"host": "pc.tail1.ts.net"})
    assert remote_push.vapid_sub() == "https://pc.tail1.ts.net"
    monkeypatch.setattr(remote_push, "_vapid_sub", None)
    monkeypatch.setattr(router_mod, "tailscale_state", lambda: {"host": None})
    assert remote_push.vapid_sub().startswith("https://")


def test_a_listener_is_not_a_subscriber():
    from app.awareness.monitor import AwarenessMonitor
    from app.awareness.observations import Observation, Severity

    monitor = AwarenessMonitor()
    seen = []
    monitor.add_listener(seen.append)
    monitor.emit(Observation(kind="routine:a", severity=Severity.INFO, title="Stretch"))
    assert seen and seen[0]["kind"] == "routine:a"
    assert not monitor.has_subscribers


def test_push_endpoints_work_for_a_paired_device_only(devices, monkeypatch):
    import app.main as main
    from app.routers import remote as router_mod

    device = _paired(devices)
    client = TestClient(main.app)
    assert client.post("/api/remote/push", json=SUB).status_code == 400  # the desktop has no subscription
    monkeypatch.setattr(router_mod, "remote_device", lambda request: device)
    assert client.post("/api/remote/push", json={**SUB, "endpoint": "http://evil.example/x"}).status_code == 400
    assert client.post("/api/remote/push", json=SUB).json() == {"notifications": True}
    assert devices.push_targets() == [(device["id"], SUB)]
    monkeypatch.setattr(remote_push, "send", lambda sub, msg: 201)
    assert client.post("/api/remote/push/test").json() == {"sent": True}
    monkeypatch.setattr(remote_push, "send", lambda sub, msg: None)
    assert client.post("/api/remote/push/test").status_code == 502
    assert client.delete("/api/remote/push").json() == {"notifications": False}
    assert devices.push_targets() == []
    assert len(client.get("/api/remote/push/key").json()["public_key"]) == 87
