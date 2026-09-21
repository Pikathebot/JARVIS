import io
import logging
import os
import tempfile
from typing import Any, Optional

logger = logging.getLogger("jarvis.voice.transcriber")


class AudioTranscriber:
    """
    Speech-to-Text transcription engine powered by local Whisper.
    """

    def __init__(self, model_size: str = "tiny.en"):
        self.model_size = model_size
        self._whisper_model = None

    def _get_model(self):
        if self._whisper_model is None:
            try:
                from faster_whisper import WhisperModel
                logger.info("Loading local Whisper model ('%s' on CPU)...", self.model_size)
                self._whisper_model = WhisperModel(
                    self.model_size,
                    device="cpu",
                    compute_type="int8"
                )
            except Exception as e:
                logger.warning("Failed to initialize Whisper model: %s", e)
                return None
        return self._whisper_model

    def transcribe_audio_bytes(self, audio_data: bytes, format: str = "webm") -> dict[str, Any]:
        """
        Transcribe raw audio bytes (webm, wav, etc.) into clean text.
        """
        if not audio_data or len(audio_data) == 0:
            return {"text": "", "duration_seconds": 0.0, "confidence": 0.0, "format": format}

        model = self._get_model()
        if model is None:
            return {"text": "", "duration_seconds": 0.0, "confidence": 0.0, "format": format, "error": "Whisper unavailable"}

        # Write to temporary file for whisper ingestion
        suffix = f".{format}" if not format.startswith(".") else format
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp_file:
            tmp_path = tmp_file.name
            tmp_file.write(audio_data)

        try:
            # The name is out-of-vocabulary for tiny.en; priming the decoder with it is the
            # difference between "Jarvis, what's the time" and "Oh, Jarmes. What's the time?".
            segments, info = model.transcribe(
                tmp_path,
                beam_size=2,
                initial_prompt="Jarvis, what's the time? Hey Jarvis.",
                vad_filter=True,
            )
            full_text = " ".join([seg.text.strip() for seg in segments]).strip()
            
            # Clean wake-word prefixes
            lower = full_text.lower()
            wake_words = ["hey jarvis", "ok jarvis", "okay jarvis", "hello jarvis", "jarvis"]
            cleaned = full_text
            for w in wake_words:
                if lower.startswith(w):
                    cleaned = full_text[len(w):].strip().lstrip(",.?! ")
                    break

            logger.info("Transcribed audio (%d bytes): '%s'", len(audio_data), cleaned or full_text)
            return {
                "text": cleaned or full_text,
                "raw_text": full_text,
                "duration_seconds": getattr(info, "duration", 0.0),
                "language": getattr(info, "language", "en"),
                "confidence": 0.95,
                "format": format
            }
        except Exception as e:
            logger.error("Whisper transcription error: %s", e)
            return {
                "text": "",
                "duration_seconds": round(len(audio_data) / 32000.0, 2) if audio_data else 0.0,
                "confidence": 0.0,
                "format": format,
                "error": str(e)
            }


        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

    def transcribe_text_stream(self, raw_transcript: str) -> str:
        """
        Clean and normalize raw speech recognition text.
        """
        return raw_transcript.strip()
