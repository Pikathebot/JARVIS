"""Voice from the phone (PLAN 4.19 phase 3): a recorded message becomes words and nothing else, a
reply becomes the persona's audio, and neither touches the desktop's hands-free voice state."""
import base64

import pytest
from fastapi.testclient import TestClient

from app.remote import remote_route_allowed


class FakeTranscriber:
    def __init__(self):
        self.calls = []

    def transcribe_audio_bytes(self, data, format="webm"):
        self.calls.append((data, format))
        return {"text": "what's the time", "raw_text": "Jarvis, what's the time", "duration_seconds": 1.4}


class FakeSynthesizer:
    def __init__(self, audio=b"ID3fake"):
        self.audio, self.said = audio, []

    async def generate_neural_audio_bytes(self, text, voice=None):
        self.said.append((text, voice))
        return self.audio


@pytest.fixture
def client(monkeypatch):
    import app.main as main
    from app.routers import voice as voice_router

    transcriber, synthesizer = FakeTranscriber(), FakeSynthesizer()
    monkeypatch.setattr(voice_router, "get_transcriber", lambda: transcriber)
    monkeypatch.setattr(voice_router, "get_synthesizer", lambda: synthesizer)
    c = TestClient(main.app)
    c.transcriber, c.synthesizer = transcriber, synthesizer
    return c


def test_the_phone_may_reach_its_voice_routes_but_not_the_desktops():
    assert remote_route_allowed("POST", "/api/remote/transcribe")
    assert remote_route_allowed("POST", "/api/remote/say")
    for path in ("/api/voice/listen", "/api/voice/say", "/api/voice/mode", "/api/voice/session/x/start"):
        assert not remote_route_allowed("POST", path), path


def test_the_phone_app_is_revalidated_on_every_load(client):
    # A stale app.js under a new index.html left Android's mic button dead (2026-10-21).
    for path in ("/m/app.js", "/m/index.html", "/m/sw.js"):
        res = client.get(path)
        assert res.status_code == 200 and res.headers["cache-control"] == "no-cache", path


def test_a_recording_comes_back_as_words_only(client):
    from app.routers.voice import voice_sessions

    before = voice_sessions.get("jarvis-thread").to_dict()
    res = client.post("/api/remote/transcribe",
                      files={"file": ("message.mp4", b"\x00\x00\x00\x18ftypmp42", "audio/mp4")})
    assert res.status_code == 200
    body = res.json()
    assert body["text"] == "what's the time"  # the wake word is dropped: it's a message, not a summons
    assert body["duration_seconds"] == 1.4 and body["error"] is None
    assert client.transcriber.calls[0][1] == "mp4"  # Safari's AAC is decoded as what it is
    assert voice_sessions.get("jarvis-thread").to_dict() == before  # the desktop's voice state is untouched


@pytest.mark.parametrize("name,ctype,fmt", [
    ("blob", "audio/webm;codecs=opus", "webm"),  # Chrome on Android, no extension
    ("clip.ogg", "application/octet-stream", "ogg"),
    ("blob", "application/octet-stream", "webm"),
])
def test_the_clip_format_comes_from_the_name_or_the_type(client, name, ctype, fmt):
    client.post("/api/remote/transcribe", files={"file": (name, b"abc", ctype)})
    assert client.transcriber.calls[-1][1] == fmt


def test_empty_and_oversized_recordings_are_refused(client, monkeypatch):
    from app.routers import remote as router_mod

    assert client.post("/api/remote/transcribe", files={"file": ("a.webm", b"", "audio/webm")}).status_code == 400
    monkeypatch.setattr(router_mod, "_MAX_CLIP_BYTES", 4)
    assert client.post("/api/remote/transcribe", files={"file": ("a.webm", b"12345", "audio/webm")}).status_code == 413
    assert client.transcriber.calls == []


def test_a_reply_is_spoken_in_the_personas_voice(client):
    from app.persona import persona_manager

    res = client.post("/api/remote/say", json={"text": "**Volume's** at 30."})
    assert res.status_code == 200
    body = res.json()
    spoken, voice = client.synthesizer.said[0]
    assert body["spoken_text"] == spoken and "**" not in spoken  # shaped for speech, markdown gone
    assert voice == persona_manager.get_active().voice_id
    assert base64.b64decode(body["audio_base64"]) == b"ID3fake" and body["audio_mime"] == "audio/mpeg"


def test_nothing_sayable_gives_no_audio(client, monkeypatch):
    from app.persona import persona_manager

    monkeypatch.setattr(type(persona_manager.get_active()), "shape_for_speech", lambda self, text: "")
    assert client.post("/api/remote/say", json={"text": "```\ncode\n```"}).json()["audio_base64"] == ""
    assert client.synthesizer.said == []
    assert client.post("/api/remote/say", json={"text": ""}).status_code == 422
