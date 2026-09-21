"""Local speech: Kokoro on the CPU behind VoiceSynthesizer, MP3 on the wire, no cloud unless asked."""

import asyncio
from pathlib import Path

import numpy as np
import pytest

from app.voice.kokoro_engine import KOKORO_VOICES, KokoroEngine, SAMPLE_RATE, encode_mp3
from app.voice.synthesizer import VoiceSynthesizer


# ------------------------------------------------------------------ engine, no model needed

def test_voice_keys_resolve_to_kokoro_ids():
    for key, kid in KOKORO_VOICES.items():
        assert KokoroEngine.resolve_voice(key) == kid
    assert KokoroEngine.resolve_voice("bm_lewis") == "bm_lewis"          # raw id passes through
    assert KokoroEngine.resolve_voice(None) == KOKORO_VOICES["jarvis-british"]
    # An edge-tts id from an old persona override keeps its accent rather than failing.
    assert KokoroEngine.resolve_voice("en-GB-RyanNeural") == KOKORO_VOICES["jarvis-british"]
    assert KokoroEngine.resolve_voice("en-US-JennyNeural") == KOKORO_VOICES["jarvis-american"]


def test_encode_mp3_produces_mpeg_frames_of_the_right_length():
    t = np.linspace(0, 1.0, SAMPLE_RATE, endpoint=False)
    pcm = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    mp3 = encode_mp3(pcm, SAMPLE_RATE)
    assert mp3[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")  # MPEG audio frame sync
    import soundfile as sf
    import io
    decoded, sr = sf.read(io.BytesIO(mp3))
    assert sr == SAMPLE_RATE
    assert abs(len(decoded) / sr - 1.0) < 0.1


def test_engine_reports_missing_files_without_loading(tmp_path):
    engine = KokoroEngine(tmp_path / "nope.onnx", tmp_path / "nope.bin")
    assert not engine.is_available()
    assert not engine.is_loaded()
    with pytest.raises(FileNotFoundError):
        engine.synthesize_pcm("hello")


# ------------------------------------------------------------------ synthesizer policy

class _StubKokoro:
    def __init__(self, available=True, fail=False):
        self.available = available
        self.fail = fail
        self.model_path = Path("stub.onnx")
        self.calls = []

    def is_available(self):
        return self.available

    def synthesize_mp3(self, text, voice=None, speed=None):
        self.calls.append((text, voice))
        if self.fail:
            raise RuntimeError("boom")
        return b"\xff\xf3mp3"


def _synth(**kw):
    return VoiceSynthesizer(backend=kw.pop("backend", "kokoro"), cloud_fallback=kw.pop("cloud_fallback", False),
                            kokoro=kw.pop("kokoro", _StubKokoro()))


def test_kokoro_is_used_and_text_is_sanitised_first():
    stub = _StubKokoro()
    synth = _synth(kokoro=stub)
    assert synth.active_backend == "kokoro"
    out = asyncio.run(synth.generate_neural_audio_bytes("**Bold** and `code`.", voice="natural-female"))
    assert out == b"\xff\xf3mp3"
    text, voice = stub.calls[0]
    assert "**" not in text and "`" not in text
    assert voice == "natural-female"


def test_no_cloud_unless_fallback_is_explicitly_allowed(monkeypatch):
    edge_calls = []

    async def fake_edge(self, text, voice):
        edge_calls.append(voice)
        return b"edge"

    monkeypatch.setattr(VoiceSynthesizer, "_edge_tts", fake_edge)

    # Files missing, fallback off: silence, and edge-tts is never contacted.
    synth = _synth(kokoro=_StubKokoro(available=False))
    assert synth.active_backend == "none"
    assert asyncio.run(synth.generate_neural_audio_bytes("hello")) == b""
    assert edge_calls == []

    # Synthesis error, fallback off: same.
    synth = _synth(kokoro=_StubKokoro(fail=True))
    assert asyncio.run(synth.generate_neural_audio_bytes("hello")) == b""
    assert edge_calls == []

    # Fallback explicitly on: the cloud path is taken with the same voice key.
    synth = _synth(kokoro=_StubKokoro(available=False), cloud_fallback=True)
    assert synth.active_backend == "edge"
    assert asyncio.run(synth.generate_neural_audio_bytes("hello", voice="jarvis-british")) == b"edge"
    assert edge_calls == ["jarvis-british"]

    # Backend "edge" chosen outright.
    synth = _synth(backend="edge")
    assert asyncio.run(synth.generate_neural_audio_bytes("hello")) == b"edge"


# ------------------------------------------------------------------ real model, when present

@pytest.mark.skipif(not KokoroEngine(
    Path(__file__).resolve().parents[2] / "models" / "tts" / "kokoro" / "kokoro-v1.0.onnx",
    Path(__file__).resolve().parents[2] / "models" / "tts" / "kokoro" / "voices-v1.0.bin",
).is_available(), reason="Kokoro model files not downloaded")
def test_real_kokoro_speaks_british_english():
    root = Path(__file__).resolve().parents[2] / "models" / "tts" / "kokoro"
    engine = KokoroEngine(root / "kokoro-v1.0.onnx", root / "voices-v1.0.bin")
    pcm = engine.synthesize_pcm("The workspace is ready, sir.", voice="jarvis-british")
    seconds = len(pcm) / SAMPLE_RATE
    assert 1.0 < seconds < 5.0
    assert float(np.abs(pcm).max()) > 0.05
    mp3 = engine.synthesize_mp3("Ready.", voice="jarvis-british")
    assert mp3[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")
