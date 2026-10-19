"""PLAN 4.16 phase B: tasks.md <-> Google Tasks, two-way, against a fake Google Tasks API."""
import json
import os
import time
from datetime import datetime

import httpx
import pytest

from app.agent.tools import notes as nm
from app.config import settings
from app.google import auth, tasks_sync


class FakeGoogleTasks:
    def __init__(self):
        self.tasks: dict[str, dict] = {}
        self.n = 0
        self.calls = []

    def add(self, title, status="needsAction", due=None):
        self.n += 1
        gid = f"g{self.n}"
        self.tasks[gid] = {"id": gid, "title": title, "status": status, "due": due,
                           "updated": "2026-10-18T10:00:00.000Z"}
        return gid

    def edit(self, gid, **fields):
        self.tasks[gid].update(fields, updated=datetime.utcnow().isoformat() + "Z")

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append((req.method, req.url.path))
        base = "/tasks/v1/lists/@default/tasks"
        if req.method == "GET" and req.url.path == base:
            return httpx.Response(200, json={"items": list(self.tasks.values())})
        if req.method == "POST" and req.url.path == base:
            body = json.loads(req.content)
            gid = self.add(body["title"], body.get("status", "needsAction"), body.get("due"))
            return httpx.Response(200, json=self.tasks[gid])
        gid = req.url.path.rsplit("/", 1)[-1]
        if gid not in self.tasks:
            return httpx.Response(404, json={})
        if req.method == "PATCH":
            self.tasks[gid].update(json.loads(req.content), updated=datetime.utcnow().isoformat() + "Z")
            return httpx.Response(200, json=self.tasks[gid])
        if req.method == "DELETE":
            del self.tasks[gid]
            return httpx.Response(204)
        return httpx.Response(400)


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "notes_dir", str(tmp_path / "notes"))
    monkeypatch.setattr(auth, "DATA_DIR", tmp_path / "google")
    monkeypatch.setattr(auth, "access_token", lambda: "tok")
    monkeypatch.setattr(auth, "status", lambda: {"signed_in": True})
    monkeypatch.setattr(nm, "_scheduler", lambda: type("S", (), {"create": lambda s, r: r, "delete": lambda s, i: True})())
    monkeypatch.setattr(tasks_sync, "sync_soon", lambda: None)
    fake = FakeGoogleTasks()
    monkeypatch.setattr(auth, "_http", lambda: httpx.Client(transport=httpx.MockTransport(fake.handler)))
    tasks_sync._last_run[0] = 0.0
    return fake


def _md():
    return (nm.notes_dir() / "tasks.md").read_text(encoding="utf-8")


def _open_titles(fake):
    return sorted(g["title"] for g in fake.tasks.values() if g["status"] != "completed")


def test_first_sync_merges_both_sides(world):
    nm._tasks("add", "Buy milk", "friday", "", None)
    world.add("Call mum")
    n = tasks_sync.sync()
    assert n["created_there"] == 1 and n["created_here"] == 1
    assert _open_titles(world) == ["Buy milk", "Call mum"]
    assert "- [ ] Call mum <!-- id:" in _md()
    milk = [g for g in world.tasks.values() if g["title"] == "Buy milk"][0]
    assert milk["due"].startswith("2026-") and milk["due"].endswith("T00:00:00.000Z")
    assert tasks_sync.sync() == dict.fromkeys(n, 0)  # nothing new: a second sync moves nothing


def test_changes_flow_each_way(world):
    nm._tasks("add", "Buy milk", "", "", None)
    gid = world.add("Call mum")
    tasks_sync.sync()
    nm._tasks("done", "", "", "milk", None)          # ticked off here
    world.edit(gid, title="Call mum about Sunday")  # renamed at Google
    n = tasks_sync.sync()
    assert n["to_google"] == 1 and n["from_google"] == 1
    assert [g["status"] for g in world.tasks.values() if g["title"] == "Buy milk"] == ["completed"]
    assert "- [ ] Call mum about Sunday <!--" in _md()


def test_deletions_flow_each_way(world):
    nm._tasks("add", "Buy milk", "", "", None)
    gid = world.add("Call mum")
    tasks_sync.sync()
    nm._tasks("remove", "", "", "milk", None)
    del world.tasks[gid]
    n = tasks_sync.sync()
    assert n["deleted_there"] == 1 and n["deleted_here"] == 1
    assert world.tasks == {} and "- [" not in _md()


def test_a_hand_typed_line_is_linked_and_keeps_its_link(world):
    nm.notes_dir().mkdir(parents=True)
    (nm.notes_dir() / "tasks.md").write_text("# Tasks\n\n- [ ] Play God of war 3\n", encoding="utf-8")
    tasks_sync.sync()
    assert _open_titles(world) == ["Play God of war 3"] and "<!-- id:t" in _md()
    assert tasks_sync.sync()["created_there"] == 0  # same line, same link: no duplicate at Google


def test_both_sides_changed_the_newer_wins(world):
    nm._tasks("add", "Buy milk", "", "", None)
    tasks_sync.sync()
    gid = next(iter(world.tasks))
    nm._tasks("edit", "Buy oat milk", "", "milk", None)
    p = nm.notes_dir() / "tasks.md"
    os.utime(p, (time.time() - 3600, time.time() - 3600))  # the local edit is an hour old
    world.edit(gid, title="Buy milk and eggs")                 # Google's is now
    tasks_sync.sync()
    assert "- [ ] Buy milk and eggs <!--" in _md() and _open_titles(world) == ["Buy milk and eggs"]


def test_maybe_sync_never_raises_and_waits_a_minute(world, monkeypatch):
    def boom():
        raise httpx.ConnectError("offline")
    monkeypatch.setattr(tasks_sync, "sync", boom)
    assert tasks_sync.maybe_sync() is None              # offline: skipped quietly
    assert tasks_sync.maybe_sync() is None and tasks_sync._last_run[0] > 0
    monkeypatch.setattr(auth, "status", lambda: {"signed_in": False})
    tasks_sync._last_run[0] = 0.0
    assert tasks_sync.maybe_sync() is None              # signed out: never calls Google


def test_a_task_completed_at_google_keeps_googles_date(world):
    gid = world.add("Renew passport", status="completed")
    world.tasks[gid]["completed"] = "2025-03-04T10:00:00.000Z"
    tasks_sync.sync()
    assert "Renew passport (done 2025-03-04)" in _md()
    tasks_sync.sync()
    assert "Renew passport (done 2025-03-04)" in _md()
