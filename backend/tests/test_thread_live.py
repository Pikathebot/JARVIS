"""The thread live relay (PLAN 4.19): a thread turn streams to every other open device as it happens."""
from fastapi.testclient import TestClient

from app.remote import remote_route_allowed
from app.thread_live import ThreadLive, get_thread_live


def _fake_turn(monkeypatch, events):
    import app.main as main

    class FakeOrchestrator:
        def __init__(self, **kwargs):
            pass

        async def run_stream(self, **kwargs):
            for event in events:
                yield event

    monkeypatch.setattr(main, "AgentOrchestrator", FakeOrchestrator)
    monkeypatch.setattr(main.settings, "governor_enabled", False)
    return main


def _drain(queue):
    frames = []
    while not queue.empty():
        frames.append(queue.get_nowait())
    return frames


def test_a_thread_turn_is_relayed_with_its_turn_and_origin(monkeypatch):
    main = _fake_turn(monkeypatch, [
        {"event": "tool_start", "data": {"tool": "web_search", "args": {"query": "x"}}},
        {"event": "token", "data": {"delta": "Hi"}},
        {"event": "done", "data": {"response": "Hi"}},
    ])
    hub = get_thread_live()
    queue = hub.subscribe()
    try:
        r = TestClient(main.app).post("/chat/stream", headers={"X-Jarvis-Client": "phone-1"},
                                      json={"message": "hello", "session_id": "jarvis-thread", "chat_mode": "FREEFORM"})
        assert r.status_code == 200 and "event: done" in r.text
        frames = _drain(queue)
    finally:
        hub.unsubscribe(queue)
    assert [e for e, _ in frames] == ["turn_start", "tool_start", "token", "done", "turn_end"]
    assert frames[0][1]["message"] == "hello" and frames[0][1]["resumed"] is False
    assert {d["_origin"] for _, d in frames} == {"phone-1"}
    assert len({d["_turn"] for _, d in frames}) == 1
    assert frames[2][1]["delta"] == "Hi"


def test_an_approval_resume_is_marked_and_side_chats_are_not_relayed(monkeypatch):
    main = _fake_turn(monkeypatch, [{"event": "done", "data": {"response": "ok"}}])
    hub = get_thread_live()
    queue = hub.subscribe()
    try:
        client = TestClient(main.app)
        client.post("/chat/stream", json={"message": "open spotify", "session_id": "jarvis-thread",
                                          "chat_mode": "FREEFORM", "approved_action_ids": ["act_0123456789abcdef"]})
        resumed = _drain(queue)
        client.post("/chat/stream", json={"message": "side", "session_id": "side-chat", "chat_mode": "FREEFORM"})
        client.post("/chat/stream", json={"message": "scratch", "session_id": "jarvis-thread",
                                          "chat_mode": "FREEFORM", "ephemeral": True})
        others = _drain(queue)
    finally:
        hub.unsubscribe(queue)
    assert resumed[0][0] == "turn_start" and resumed[0][1]["resumed"] is True
    assert others == []


def test_a_stalled_subscriber_is_dropped_not_waited_on(monkeypatch):
    import app.thread_live as live_mod

    monkeypatch.setattr(live_mod, "_QUEUE_SIZE", 3)
    hub = ThreadLive()
    queue = hub.subscribe()
    for i in range(10):
        hub.publish("token", {"delta": str(i)}, "t1", "phone")
    assert hub.subscriber_count == 0 and queue.full()


def test_the_phone_may_listen():
    assert remote_route_allowed("GET", "/api/thread/live")
