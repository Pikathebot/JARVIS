import io
import time

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.voice.session import VoiceSessionManager, VoiceState


@pytest.fixture
def manager():
    return VoiceSessionManager(follow_up_window_seconds=10.0)


@pytest.fixture
def client(monkeypatch):
    """A client with a fresh voice state machine and a stubbed transcriber."""
    import app.routers.voice as voice_router

    monkeypatch.setattr(
        voice_router, "voice_sessions", VoiceSessionManager(follow_up_window_seconds=10.0)
    )
    return TestClient(app)


def stub_transcript(monkeypatch, text: str, **extra):
    import app.routers.voice as voice_router

    class _Stub:
        def transcribe_audio_bytes(self, data, format="webm"):
            return {"text": text, "raw_text": text, "duration_seconds": 1.0, **extra}

    monkeypatch.setattr(voice_router, "get_transcriber", lambda: _Stub())


def audio_upload(name: str = "chunk.webm"):
    return {"file": (name, io.BytesIO(b"fake-audio-bytes"), "audio/webm")}


# ------------------------------------------------------------ state machine


def test_utterance_without_wake_word_is_ignored(manager):
    decision = manager.evaluate_utterance("s1", "what time does the train leave")

    assert decision.should_respond is False
    assert decision.reason == "no_wake_word"


def test_wake_word_with_command_dispatches_only_the_command(manager):
    decision = manager.evaluate_utterance("s1", "Jarvis, what is the GPU temperature")

    assert decision.should_respond is True
    assert decision.query == "what is the GPU temperature"
    assert decision.wake_detected is True
    assert decision.state == VoiceState.THINKING


def test_bare_wake_word_arms_and_speaks_greeting(manager):
    decision = manager.evaluate_utterance("s1", "Hey Jarvis", greeting="At your service, sir.")

    assert decision.should_respond is False
    assert decision.reason == "wake_word_awaiting_command"
    assert decision.speak_immediately == "At your service, sir."
    assert manager.get("s1").is_armed()

    # The next utterance needs no wake word.
    follow_up = manager.evaluate_utterance("s1", "open the project folder")
    assert follow_up.should_respond is True
    assert follow_up.query == "open the project folder"


def test_follow_up_window_expires(manager):
    manager.evaluate_utterance("s1", "Jarvis")
    manager.get("s1").armed_until = time.time() - 1

    decision = manager.evaluate_utterance("s1", "and the disk usage")
    assert decision.should_respond is False
    assert decision.reason == "no_wake_word"


def test_arming_is_consumed_by_the_utterance_it_serves(manager):
    manager.arm_follow_up("s1")
    assert manager.evaluate_utterance("s1", "run the tests").should_respond is True

    # Without re-arming, the next stray utterance is ignored again.
    assert manager.evaluate_utterance("s1", "hmm okay").should_respond is False


def test_empty_and_noise_transcripts_are_dropped(manager):
    manager.arm_follow_up("s1")

    for noise in ("", "   ", "a"):
        decision = manager.evaluate_utterance("s1", noise)
        assert decision.should_respond is False
        assert decision.reason == "empty_transcript"

    # A dropped chunk must not consume the arming.
    assert manager.get("s1").is_armed()


class _Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def test_follow_up_window_grows_while_talking_and_resets_after_a_lapse(manager, monkeypatch):
    """The user's rule (2026-10-13): talking often needs no wake word; a long quiet does.
    The manager fixture's base window is 10 s, so the rungs are 10 / 30 / 60."""
    import app.voice.session as session_mod

    clock = _Clock()
    monkeypatch.setattr(session_mod.time, "time", clock)

    def reply_then_wait(seconds):
        manager.arm_follow_up("s1")  # the client calls this when the reply finishes playing
        clock.t += seconds

    assert manager.evaluate_utterance("s1", "Jarvis, open discord").should_respond
    reply_then_wait(8)  # rung 0: 10 s
    assert manager.evaluate_utterance("s1", "focus task manager").should_respond
    reply_then_wait(25)  # rung 1: 30 s
    assert manager.evaluate_utterance("s1", "what's the cpu at").should_respond
    reply_then_wait(52)  # rung 2: 60 s -- the gap that was ignored on device
    assert manager.evaluate_utterance("s1", "open the hud").should_respond
    reply_then_wait(65)  # past the ceiling: the conversation is over
    assert manager.evaluate_utterance("s1", "open the hud").should_respond is False
    assert manager.get("s1").streak == 0
    # Back with the wake word: a fresh conversation starts on the short rung.
    assert manager.evaluate_utterance("s1", "Jarvis, open the hud").should_respond
    reply_then_wait(15)
    assert manager.evaluate_utterance("s1", "and close it").should_respond is False


def test_a_sentence_begun_inside_the_window_counts(manager, monkeypatch):
    import app.voice.session as session_mod

    clock = _Clock()
    monkeypatch.setattr(session_mod.time, "time", clock)
    manager.arm_follow_up("s1")
    clock.t += 12  # 2 s past the window when the upload lands...
    assert manager.evaluate_utterance("s1", "search for the mod link", spoken_seconds=3.5).should_respond


def test_speech_while_jarvis_is_answering_does_not_end_the_conversation(manager):
    manager.arm_follow_up("s1")
    manager.evaluate_utterance("s1", "what's the weather")  # in the window: streak 1, now THINKING
    assert manager.evaluate_utterance("s1", "uh huh").should_respond is False
    assert manager.get("s1").streak == 1


def test_sessions_are_isolated(manager):
    manager.evaluate_utterance("s1", "Jarvis")

    assert manager.get("s1").is_armed()
    assert not manager.get("s2").is_armed()
    assert manager.evaluate_utterance("s2", "run the tests").should_respond is False


def test_stop_listening_clears_arming(manager):
    manager.evaluate_utterance("s1", "Jarvis")
    manager.stop_listening("s1")

    session = manager.get("s1")
    assert session.state == VoiceState.IDLE
    assert not session.is_armed()
    assert manager.evaluate_utterance("s1", "run the tests").should_respond is False


def test_turn_counter_tracks_dispatched_requests(manager):
    manager.evaluate_utterance("s1", "Jarvis, one")
    manager.arm_follow_up("s1")
    manager.evaluate_utterance("s1", "two")
    manager.evaluate_utterance("s1", "ignored, not addressed")

    assert manager.get("s1").turns == 2


# --------------------------------------------------------------------- API


def test_listen_reports_wake_word_command(client, monkeypatch):
    stub_transcript(monkeypatch, "Jarvis, what is the GPU temperature")

    res = client.post("/api/voice/listen", files=audio_upload(), data={"session_id": "api1"})
    assert res.status_code == 200

    body = res.json()
    assert body["should_respond"] is True
    assert body["query"] == "what is the GPU temperature"
    assert body["session"]["turns"] == 1


def test_listen_ignores_unaddressed_speech(client, monkeypatch):
    stub_transcript(monkeypatch, "so anyway I told him it was fine")

    body = client.post(
        "/api/voice/listen", files=audio_upload(), data={"session_id": "api2"}
    ).json()

    assert body["should_respond"] is False
    assert body["reason"] == "no_wake_word"


def test_listen_rejects_empty_upload(client):
    res = client.post(
        "/api/voice/listen",
        files={"file": ("chunk.webm", io.BytesIO(b""), "audio/webm")},
        data={"session_id": "api3"},
    )
    assert res.status_code == 400


def test_say_shapes_text_and_returns_audio(client, monkeypatch):
    import app.routers.voice as voice_router

    class _Synth:
        async def generate_neural_audio_bytes(self, text, voice=None):
            return b"ID3-fake-mp3"

    monkeypatch.setattr(voice_router, "get_synthesizer", lambda: _Synth())

    body = client.post(
        "/api/voice/say",
        json={"text": "**Done.** ```py\nx=1\n``` It works.", "session_id": "api4"},
    ).json()

    assert "**" not in body["spoken_text"]
    assert "x=1" not in body["spoken_text"]
    assert body["audio_base64"]
    assert body["synthesis_failed"] is False
    assert body["session"]["state"] == "speaking"


def test_say_reports_synthesis_failure_without_erroring(client, monkeypatch):
    import app.routers.voice as voice_router

    class _Synth:
        async def generate_neural_audio_bytes(self, text, voice=None):
            return b""

    monkeypatch.setattr(voice_router, "get_synthesizer", lambda: _Synth())

    body = client.post("/api/voice/say", json={"text": "Hello.", "session_id": "api5"}).json()

    assert body["synthesis_failed"] is True
    assert body["spoken_text"] == "Hello."


def test_session_lifecycle_endpoints(client):
    assert client.post("/api/voice/session/api6/start").json()["state"] == "listening"
    assert client.post("/api/voice/session/api6/arm").json()["armed"] is True

    # Barge-in: the client reports it went back to listening mid-reply.
    assert (
        client.post("/api/voice/session/api6/state", json={"state": "listening"}).json()["state"]
        == "listening"
    )
    assert client.post("/api/voice/session/api6/stop").json()["armed"] is False


def test_unknown_voice_state_is_rejected(client):
    res = client.post("/api/voice/session/api7/state", json={"state": "daydreaming"})
    assert res.status_code == 400


def test_hands_free_status_exposes_wake_words_and_voice(client):
    body = client.get("/api/voice/hands-free").json()

    assert "jarvis" in body["wake_words"]
    assert body["follow_up_window_seconds"] == 10.0
    assert body["voice_id"]
