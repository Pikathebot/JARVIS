import pytest
import httpx
from app.main import app
from app.voice.wake_word import WakeWordDetector
from app.voice.transcriber import AudioTranscriber
from app.voice.synthesizer import VoiceSynthesizer


# --- Unit Tests for Voice Subsystems ---

def test_wake_word_detector():
    wakes = []

    def on_wake_triggered(query):
        wakes.append(query)

    detector = WakeWordDetector(on_wake=on_wake_triggered)

    # 1. Direct wake word
    detected, word, query = detector.detect_in_text("Jarvis, what time is it?")
    assert detected is True
    assert word.lower() == "jarvis"
    assert query == "what time is it?"
    assert len(wakes) == 1

    # 2. Multi-word wake phrase
    detected2, word2, query2 = detector.detect_in_text("Hey Jarvis! List the files in docs.")
    assert detected2 is True
    assert "jarvis" in word2.lower()
    assert "List the files in docs" in query2

    # 3. No wake word
    detected3, _, _ = detector.detect_in_text("Tell me a funny story.")
    assert detected3 is False


def test_wake_word_only_at_the_start():
    # Device 2026-10-20: reading "send a draft email ... about JARVIS app" aloud while typing it
    # fired "app" as a command. The name only wakes Jarvis at the start of what was said.
    detector = WakeWordDetector()
    assert detector.detect_in_text("send a draft email to me about JARVIS app")[0] is False
    assert detector.detect_in_text("I think Jarvis is slow today")[0] is False
    detected, _, query = detector.detect_in_text("... Jarvis, open Steam.")
    assert detected is True and query == "open Steam."
    assert detector.detect_in_text("Okay Jarvis mute the volume")[2] == "mute the volume"
    # A misheard name at the start still counts (aliases are rewritten first).
    assert detector.detect_in_text("Jervis what's the time")[0] is True


def test_voice_synthesizer_sanitization():
    synth = VoiceSynthesizer()
    raw_markdown = (
        "# System Health\n"
        "Here is the status: **Online**.\n"
        "```python\ndef foo(): pass\n```\n"
        "Check out `config.py` at [Link](http://example.com)."
    )

    clean = synth.sanitize_for_speech(raw_markdown)
    assert "#" not in clean
    assert "```" not in clean
    assert "**" not in clean
    assert "[code block omitted]" in clean
    assert "Online" in clean
    assert "config.py" in clean

    res = synth.synthesize(raw_markdown)
    assert "spoken_text" in res
    assert res["length_chars"] > 0


def test_audio_transcriber_processing():
    transcriber = AudioTranscriber()
    dummy_wav = b"RIFF" + b"\x00" * 100
    res = transcriber.transcribe_audio_bytes(dummy_wav, format="wav")
    assert "duration_seconds" in res
    assert res["format"] == "wav"


def test_gpu_whisper_failure_leaves_the_cpu_model_serving(monkeypatch):
    import faster_whisper

    def boom(name, device="cpu", compute_type="int8"):
        if device == "cuda":
            raise RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")
        return "cpu-model"

    monkeypatch.setattr(faster_whisper, "WhisperModel", boom)
    t = AudioTranscriber(model_size="tiny.en", gpu_model="large-v3-turbo")
    assert t.use_gpu(True) is False
    assert t.vram_mb == 0.0
    assert t._get_model() == "cpu-model"
    assert t.use_gpu(False) is True


# --- API Integration Tests with FastAPI app ---

@pytest.mark.anyio
async def test_voice_status_endpoint():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/voice/status")
    
    assert response.status_code == 200
    data = response.json()
    assert "wake_word_active" in data
    assert "wake_words" in data
    assert "jarvis" in data["wake_words"]


@pytest.mark.anyio
async def test_voice_speak_endpoint():
    payload = {"text": "Hello! Everything is **running smoothly** with `fastapi`."}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.post("/voice/speak", json=payload)
    
    assert response.status_code == 200
    data = response.json()
    assert "spoken_text" in data
    assert "**" not in data["spoken_text"]
    assert "running smoothly" in data["spoken_text"]


@pytest.mark.anyio
async def test_voice_transcribe_endpoint():
    files = {"file": ("test.wav", b"RIFF" + b"\x00" * 50, "audio/wav")}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.post("/voice/transcribe", files=files)
    
    assert response.status_code == 200
    data = response.json()
    assert "duration_seconds" in data


@pytest.mark.anyio
async def test_chat_with_wake_word_prefix():
    """Verify that a prompt starting with 'Jarvis, ...' executes smoothly and strips wake word."""
    from unittest.mock import AsyncMock, patch
    from app.agent.llamacpp_provider import LlamaCppProvider
    from app.main import run_db_migrations

    # ASGITransport skips startup, so the schema is only there if an earlier test file made it;
    # run on its own this file hit "no such table: sessions".
    run_db_migrations()

    mock_resp = {
        "message": {
            "role": "assistant",
            "content": "HELLO_VOICE_CONFIRMED",
            "tool_calls": None
        },
        "raw": {}
    }
    with patch.object(LlamaCppProvider, "chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = mock_resp
        payload = {
            "message": "Jarvis, say 'HELLO_VOICE_CONFIRMED'",
            "model": "qwen2.5:0.5b"
        }
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=10.0) as ac:
            response = await ac.post("/chat", json=payload)
        
        assert response.status_code == 200
        data = response.json()
        assert "response" in data
        assert "HELLO_VOICE_CONFIRMED" in data["response"]
