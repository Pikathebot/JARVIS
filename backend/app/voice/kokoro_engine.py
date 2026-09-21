"""
Local text-to-speech: Kokoro-82M through ONNX Runtime on the CPU.

This is what makes spoken output private. The previous path streamed every reply to
Microsoft's edge-tts service; Kokoro runs entirely on this machine, takes no VRAM (the 8 GB card
stays with the language model) and about 0.4 GB of system RAM once loaded.

Measured on the i7-14700HX with ``kokoro-v1.0.onnx`` (fp32): a 4.3 s sentence synthesises in
~2.4 s, i.e. real-time factor ~0.56. The int8 file is 5x *slower* on this CPU (no fast int8
kernels on that path), so fp32 is the one to ship. First call after loading costs a few extra
seconds of warm-up, which ``warm_up`` takes at startup rather than on the first spoken reply.

Files live in ``models/tts/kokoro/`` (gitignored, like every model):
``kokoro-v1.0.onnx`` and ``voices-v1.0.bin`` from
https://github.com/thewh1teagle/kokoro-onnx/releases/tag/model-files-v1.0

The wire format stays MP3 (``lameenc``): the WinUI client writes the bytes to a ``.mp3`` and
hands the path to MediaPlayer, so the local engine must produce what edge-tts produced.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger("jarvis.voice.kokoro")

SAMPLE_RATE = 24000

# Jarvis voice keys -> Kokoro voice ids. Keys are what personas and the client know
# (``persona.voice_id``); the same keys map to edge-tts ids in ``synthesizer.py`` so a persona
# does not care which engine is behind it. British "b*", American "a*"; "m"/"f" for the speaker.
KOKORO_VOICES = {
    "jarvis-british": "bm_george",
    "jarvis-american": "am_michael",
    "natural-male": "am_adam",
    "natural-female": "af_heart",
    "british-female": "bf_emma",
}


def _lang_for(voice: str) -> str:
    return "en-gb" if voice.startswith("b") else "en-us"


class KokoroEngine:
    """Lazy, thread-safe wrapper around ``kokoro_onnx.Kokoro``."""

    def __init__(self, model_path: Path | str, voices_path: Path | str, speed: float = 1.0):
        self.model_path = Path(model_path)
        self.voices_path = Path(voices_path)
        self.speed = speed
        self._kokoro = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ lifecycle

    def is_available(self) -> bool:
        """The model files are on disk (loading is deferred to the first synthesis)."""
        return self.model_path.is_file() and self.voices_path.is_file()

    def is_loaded(self) -> bool:
        return self._kokoro is not None

    def _ensure_loaded(self):
        if self._kokoro is not None:
            return self._kokoro
        with self._lock:
            if self._kokoro is None:
                if not self.is_available():
                    raise FileNotFoundError(
                        f"Kokoro model files missing: {self.model_path} / {self.voices_path}"
                    )
                from kokoro_onnx import Kokoro

                logger.info("Loading Kokoro TTS from %s", self.model_path)
                self._kokoro = Kokoro(str(self.model_path), str(self.voices_path))
        return self._kokoro

    def warm_up(self) -> None:
        """Load and run one short utterance so the first real reply is not the slow one."""
        try:
            self.synthesize_pcm("Ready.", voice="jarvis-british")
        except Exception as exc:  # pragma: no cover - startup nicety only
            logger.warning("Kokoro warm-up skipped: %s", exc)

    def unload(self) -> None:
        with self._lock:
            self._kokoro = None

    # ------------------------------------------------------------------ synthesis

    @staticmethod
    def resolve_voice(voice: Optional[str]) -> str:
        """A Jarvis voice key, a raw Kokoro id, or anything else -> a Kokoro id."""
        if not voice:
            return KOKORO_VOICES["jarvis-british"]
        if voice in KOKORO_VOICES:
            return KOKORO_VOICES[voice]
        if voice[:3] in ("bm_", "bf_", "am_", "af_"):
            return voice
        # An edge-tts id (en-GB-*Neural) or unknown key: keep the accent at least.
        return KOKORO_VOICES["jarvis-british"] if "GB" in voice else KOKORO_VOICES["jarvis-american"]

    def synthesize_pcm(self, text: str, voice: Optional[str] = None, speed: Optional[float] = None) -> np.ndarray:
        """Mono float32 samples at ``SAMPLE_RATE``. Blocking; call off the event loop."""
        kokoro = self._ensure_loaded()
        kid = self.resolve_voice(voice)
        samples, sr = kokoro.create(text, voice=kid, speed=speed or self.speed, lang=_lang_for(kid))
        if sr != SAMPLE_RATE:  # pragma: no cover - the v1.0 model is fixed at 24 kHz
            logger.warning("Kokoro returned %d Hz, expected %d", sr, SAMPLE_RATE)
        return np.asarray(samples, dtype=np.float32)

    def synthesize_mp3(self, text: str, voice: Optional[str] = None, speed: Optional[float] = None,
                       bitrate_kbps: int = 64) -> bytes:
        """MP3 bytes, the format the client already plays."""
        pcm = self.synthesize_pcm(text, voice=voice, speed=speed)
        if pcm.size == 0:
            return b""
        return encode_mp3(pcm, SAMPLE_RATE, bitrate_kbps=bitrate_kbps)


def encode_mp3(pcm: np.ndarray, sample_rate: int, bitrate_kbps: int = 64) -> bytes:
    """Float32 mono PCM -> MP3 via LAME. Speech at 64 kbps mono is transparent enough."""
    import lameenc

    clipped = np.clip(pcm, -1.0, 1.0)
    int16 = (clipped * 32767.0).astype("<i2")
    enc = lameenc.Encoder()
    enc.set_bit_rate(bitrate_kbps)
    enc.set_in_sample_rate(sample_rate)
    enc.set_channels(1)
    enc.set_quality(2)  # 0 = best/slowest, 9 = worst/fastest; 2 is LAME's "high quality"
    return bytes(enc.encode(int16.tobytes()) + enc.flush())
