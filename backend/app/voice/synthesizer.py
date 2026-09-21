import asyncio
import logging
from typing import Any, Optional
import io

from app.config import settings
from app.persona.speech import sanitize_markdown_for_speech
from app.voice.kokoro_engine import KOKORO_VOICES, KokoroEngine

logger = logging.getLogger("jarvis.voice.synthesizer")

# Jarvis voice keys -> edge-tts ids, for the cloud backend. The same keys map to Kokoro ids in
# ``kokoro_engine.KOKORO_VOICES``; personas and the client only ever see the keys.
AVAILABLE_NEURAL_VOICES = {
    "jarvis-british": "en-GB-RyanNeural",
    "jarvis-american": "en-US-ChristopherNeural",
    "natural-male": "en-US-GuyNeural",
    "natural-female": "en-US-JennyNeural",
    "british-female": "en-GB-SoniaNeural",
}


class VoiceSynthesizer:
    """
    Text-to-speech behind ``/api/voice/say``: sanitises a reply for speaking and renders it as MP3.

    Two backends behind one interface:

    * ``kokoro`` (default) -- Kokoro-82M on the CPU through ONNX Runtime. Fully local: nothing
      leaves the machine, no VRAM. See ``kokoro_engine.py`` for the numbers.
    * ``edge`` -- Microsoft's edge-tts cloud voices, the original path. Every spoken reply is
      sent to Microsoft; kept for machines without the Kokoro files, and as an explicit opt-in
      fallback (``VOICE_TTS_CLOUD_FALLBACK``) -- off by default, because a private assistant
      should fail silent rather than fail to the cloud.
    """

    def __init__(
        self,
        voice_name: str = "jarvis-british",
        rate: str = "+0%",
        backend: Optional[str] = None,
        cloud_fallback: Optional[bool] = None,
        kokoro: Optional[KokoroEngine] = None,
    ):
        self.voice_name = voice_name
        self.rate = rate
        self.backend = (backend or settings.voice_tts_backend or "kokoro").strip().lower()
        self.cloud_fallback = settings.voice_tts_cloud_fallback if cloud_fallback is None else cloud_fallback
        self.kokoro = kokoro or KokoroEngine(
            settings.kokoro_model_path, settings.kokoro_voices_path, speed=settings.kokoro_speed
        )

    # ------------------------------------------------------------------ text

    def sanitize_for_speech(self, text: str) -> str:
        """
        Strip markdown tags, code blocks, and URLs to ensure natural vocalization.
        """
        return sanitize_markdown_for_speech(text)

    def synthesize(self, text: str) -> dict[str, Any]:
        """
        Synthesize text into a speech-ready payload.
        """
        speech_text = self.sanitize_for_speech(text)
        return {
            "spoken_text": speech_text,
            "voice": self.voice_name,
            "rate": self.rate,
            "length_chars": len(speech_text)
        }

    # ------------------------------------------------------------------ audio

    @property
    def active_backend(self) -> str:
        """Which engine a request would use right now: kokoro when its files exist, else edge
        only if the cloud fallback is allowed, else nothing."""
        if self.backend == "kokoro" and self.kokoro.is_available():
            return "kokoro"
        if self.backend == "edge" or self.cloud_fallback:
            return "edge"
        return "none"

    async def generate_neural_audio_bytes(self, text: str, voice: Optional[str] = None) -> bytes:
        """
        MP3 bytes for ``text`` in ``voice`` (a Jarvis voice key), or b"" when nothing could be
        rendered. Never raises: a spoken reply is a nicety and the text already went out.
        """
        clean_text = self.sanitize_for_speech(text)
        if not clean_text:
            return b""
        chosen_voice = voice or self.voice_name

        if self.backend == "kokoro":
            if self.kokoro.is_available():
                try:
                    return await asyncio.to_thread(self.kokoro.synthesize_mp3, clean_text, chosen_voice)
                except Exception as e:
                    logger.error("Kokoro synthesis failed: %s", e)
            else:
                logger.warning("Kokoro model files not found at %s; local speech unavailable.", self.kokoro.model_path)
            if not self.cloud_fallback:
                return b""
            logger.info("Falling back to edge-tts (VOICE_TTS_CLOUD_FALLBACK=true).")

        return await self._edge_tts(clean_text, chosen_voice)

    async def _edge_tts(self, clean_text: str, voice: str) -> bytes:
        """The original cloud path: MP3 from Microsoft's neural voices."""
        chosen_voice = AVAILABLE_NEURAL_VOICES.get(voice, voice)
        try:
            import edge_tts
            communicate = edge_tts.Communicate(clean_text, chosen_voice, rate=self.rate)
            buffer = io.BytesIO()
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    buffer.write(chunk["data"])
            return buffer.getvalue()
        except Exception as e:
            logger.error("Failed to generate edge-tts audio: %s", e)
            return b""
