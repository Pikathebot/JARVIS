import gc
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

import numpy as np

from app.voice.bark_sidecar import decode_audio, get_bark_sidecar

logger = logging.getLogger("jarvis.voice.transcriber")


def _repo_path(path_str: str) -> Path:
    p = Path(path_str)
    return p if p.is_absolute() else Path(__file__).resolve().parents[3] / p


class AudioTranscriber:
    """
    Speech-to-Text. Bark 3.0 Pro (``BarkSidecar``) is the main engine; local Whisper is the
    fallback and answers whenever Bark can't (not installed, failed to start, errored).

    :meth:`use_gpu` moves the engine onto the card -- voice mode does so while the 9B is unloaded
    and there is VRAM for it: Bark on CUDA, or, if that can't start, Whisper's larger GPU model
    (``Settings.whisper_gpu_model``) beside the CPU one. The measured VRAM is reported through
    ``vram_mb`` so the governor counts it as ours.
    """

    def __init__(self, model_size: Optional[str] = None, gpu_model: Optional[str] = None):
        from app.config import settings

        self.model_size = model_size or settings.whisper_cpu_model
        self.gpu_model_name = gpu_model or settings.whisper_gpu_model
        self._whisper_model = None
        self._gpu_model = None
        self._gpu_lock = threading.Lock()
        self.vram_mb = 0.0

    def _get_model(self):
        if self._gpu_model is not None:
            return self._gpu_model
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

    def _use_bark_gpu(self, on: bool) -> bool:
        from app.config import settings

        if settings.stt_engine != "bark":
            return False
        bark = get_bark_sidecar()
        if not on:
            if bark.device == "cuda":
                bark.stop()  # the next utterance cold-starts it on the CPU
            return True
        if bark.ensure_running("cuda"):
            self.vram_mb = bark.vram_mb
            return True
        return False

    def use_gpu(self, on: bool) -> bool:
        """Load (or drop) the GPU model. Blocking; call off the event loop. False when it
        couldn't load -- the CPU model keeps serving."""
        with self._gpu_lock:
            if self._use_bark_gpu(on):
                if not on:
                    self.vram_mb = 0.0
                    if self._gpu_model is None:
                        return True
                else:
                    return True
            if not on:
                if self._gpu_model is not None:
                    self._gpu_model = None
                    gc.collect()
                    logger.info("Whisper GPU model unloaded.")
                self.vram_mb = 0.0
                return True
            if self._gpu_model is not None:
                return True
            from app.config import settings
            from app.voice.laya_sidecar import _gpu_used_mb

            dll_dir = settings.whisper_cuda_dll_dir and _repo_path(settings.whisper_cuda_dll_dir)
            if dll_dir and dll_dir.is_dir() and hasattr(os, "add_dll_directory"):
                os.add_dll_directory(str(dll_dir))
                os.environ["PATH"] = str(dll_dir) + os.pathsep + os.environ.get("PATH", "")
            try:
                from faster_whisper import WhisperModel

                before = _gpu_used_mb()
                started = time.time()
                model = WhisperModel(self.gpu_model_name, device="cuda", compute_type="int8_float16")
                # The first encode is what pulls in cuBLAS; a missing DLL surfaces here, not above.
                list(model.transcribe(np.zeros(16000, dtype=np.float32), beam_size=1, language="en")[0])
                after = _gpu_used_mb()
                self.vram_mb = max(0.0, after - before) if (before is not None and after is not None) else 1200.0
                self._gpu_model = model
                logger.info("Whisper '%s' on GPU in %.1fs (%.0f MiB VRAM).",
                            self.gpu_model_name, time.time() - started, self.vram_mb)
                return True
            except Exception as e:
                logger.warning("Whisper GPU model unavailable, staying on CPU '%s': %s", self.model_size, e)
                self._gpu_model = None
                self.vram_mb = 0.0
                return False

    @staticmethod
    def _strip_wake_word(full_text: str) -> str:
        lower = full_text.lower()
        wake_words = ["hey jarvis", "ok jarvis", "okay jarvis", "hello jarvis", "jarvis"]
        for w in wake_words:
            if lower.startswith(w):
                return full_text[len(w):].strip().lstrip(",.?! ")
        return full_text

    def transcribe_audio_bytes(self, audio_data: bytes, format: str = "webm") -> dict[str, Any]:
        """
        Transcribe raw audio bytes (webm, wav, etc.) into clean text: Bark first, Whisper if
        Bark can't answer.
        """
        if not audio_data or len(audio_data) == 0:
            return {"text": "", "duration_seconds": 0.0, "confidence": 0.0, "format": format}

        from app.config import settings

        if settings.stt_engine == "bark":
            try:
                samples = decode_audio(audio_data)
                full_text = get_bark_sidecar().transcribe(samples)
                cleaned = self._strip_wake_word(full_text)
                logger.info("Transcribed audio (%d bytes, bark): '%s'", len(audio_data), cleaned or full_text)
                return {
                    "text": cleaned or full_text,
                    "raw_text": full_text,
                    "duration_seconds": round(len(samples) / 16000.0, 2),
                    "language": "en",
                    "confidence": 0.95,
                    "format": format,
                    "engine": "bark",
                }
            except Exception as e:
                logger.warning("Bark transcription failed, falling back to Whisper: %s", e)
        return self._transcribe_whisper(audio_data, format)

    def _transcribe_whisper(self, audio_data: bytes, format: str) -> dict[str, Any]:
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
                # The GPU model is multilingual; left to detect, an accent on a short clip can
                # be heard as another language.
                language="en",
                initial_prompt="Jarvis, what's the time? Hey Jarvis.",
                vad_filter=True,
            )
            full_text = " ".join([seg.text.strip() for seg in segments]).strip()
            
            # Clean wake-word prefixes
            cleaned = self._strip_wake_word(full_text)

            logger.info("Transcribed audio (%d bytes): '%s'", len(audio_data), cleaned or full_text)
            return {
                "text": cleaned or full_text,
                "raw_text": full_text,
                "duration_seconds": getattr(info, "duration", 0.0),
                "language": getattr(info, "language", "en"),
                "confidence": 0.95,
                "format": format,
                "engine": "whisper",
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
