import gc
import io
import logging
import re
from typing import Any, Optional, Union
import numpy as np

from app.config import settings
from app.governor.resource_governor import ResourceGovernor, ActivityType

logger = logging.getLogger("jarvis.agent.tts.chatterbox")


class ChatterboxEngine:
    """
    Chatterbox Neural TTS Engine Wrapper with Governor V2 Resource Coordination
    and local fallback cascade (Chatterbox -> Kokoro -> Text-Only).
    """

    def __init__(
        self,
        governor: Optional[ResourceGovernor] = None,
        device: Optional[str] = None,
        vram_required_mb: Optional[float] = None,
        kokoro_vram_required_mb: Optional[float] = None,
        chunk_size_chars: Optional[int] = None,
    ):
        self.governor = governor
        self.device = device or settings.tts_device
        self.vram_required_mb = vram_required_mb or settings.tts_vram_required_mb
        self.kokoro_vram_required_mb = kokoro_vram_required_mb or settings.tts_kokoro_vram_required_mb
        self.chunk_size_chars = chunk_size_chars or settings.tts_chunk_size_chars

        self._chatterbox_model: Any = None
        self._kokoro_model: Any = None
        self._active_backend: Optional[str] = None  # "chatterbox" | "kokoro" | None

    # --- 1. Lifecycle Control ---

    def _on_gpu(self) -> bool:
        return str(self.device or "").lower().startswith("cuda")

    def is_loaded(self) -> bool:
        """Reports whether any local TTS model weights are currently resident in memory/VRAM."""
        return self._chatterbox_model is not None or self._kokoro_model is not None

    def is_chatterbox_loaded(self) -> bool:
        return self._chatterbox_model is not None

    def is_kokoro_loaded(self) -> bool:
        return self._kokoro_model is not None

    def load_model(self, target_backend: str = "chatterbox") -> bool:
        """
        Explicit lifecycle control for loading TTS model weights into VRAM.
        Gated by Governor VRAM availability checks.
        """
        if target_backend == "chatterbox":
            if self._chatterbox_model is not None:
                return True

            # Governor VRAM check gate -- only meaningful for a CUDA load; on the CPU the
            # weights take system RAM and the 8 GB card is left to the language model.
            if self.governor and self._on_gpu():
                can_alloc, reason = self.governor.can_allocate_vram(self.vram_required_mb)
                if not can_alloc:
                    logger.warning(
                        "Governor VRAM gate rejected Chatterbox loading (%s). Required: %.1f MB",
                        reason,
                        self.vram_required_mb
                    )
                    return False

            try:
                logger.info("Loading Chatterbox TTS model onto device: %s...", self.device)
                from chatterbox.tts import ChatterboxTTS
                self._chatterbox_model = ChatterboxTTS.from_pretrained(device=self.device)
                self._active_backend = "chatterbox"
                logger.info("Chatterbox TTS model loaded successfully.")
                return True
            except Exception as e:
                logger.error("Failed to load Chatterbox TTS model: %s", e)
                self._chatterbox_model = None
                return False

        elif target_backend == "kokoro":
            if self._kokoro_model is not None:
                return True

            # Governor VRAM check gate for Kokoro (CUDA loads only, as above)
            if self.governor and self._on_gpu():
                can_alloc, reason = self.governor.can_allocate_vram(self.kokoro_vram_required_mb)
                if not can_alloc:
                    logger.warning(
                        "Governor VRAM gate rejected Kokoro fallback loading (%s). Required: %.1f MB",
                        reason,
                        self.kokoro_vram_required_mb
                    )
                    return False

            try:
                logger.info("Loading Kokoro TTS model...")
                try:
                    from kokoro_onnx import Kokoro
                    self._kokoro_model = Kokoro("kokoro-v0_19.onnx", "voices.bin")
                except Exception:
                    # Generic Kokoro loader fallback if different package layout
                    import kokoro
                    self._kokoro_model = kokoro
                self._active_backend = "kokoro"
                logger.info("Kokoro TTS model loaded successfully.")
                return True
            except Exception as e:
                logger.error("Failed to load Kokoro TTS model: %s", e)
                self._kokoro_model = None
                return False

        return False

    def unload_model(self) -> None:
        """
        Explicit lifecycle control for evicting TTS weights from VRAM under Governor pressure.
        """
        unloaded_any = False
        if self._chatterbox_model is not None:
            del self._chatterbox_model
            self._chatterbox_model = None
            unloaded_any = True
            logger.info("Chatterbox TTS weights evicted from memory.")

        if self._kokoro_model is not None:
            del self._kokoro_model
            self._kokoro_model = None
            unloaded_any = True
            logger.info("Kokoro TTS weights evicted from memory.")

        self._active_backend = None

        if unloaded_any:
            try:
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass
            gc.collect()
            logger.info("TTS VRAM garbage collection and cache purge completed.")

    # --- 2. Text Sanitization & Chunking ---

    def sanitize_text(self, text: str) -> str:
        """
        Strips markdown tags, URLs, tool announcements, and JSON blobs.
        Replaces code blocks with a brief verbal indicator '[code omitted]'.
        """
        if not text or not isinstance(text, str):
            return ""

        # Remove thinking blocks if present
        clean = re.sub(r"<thought>[\s\S]*?</thought>", "", text, flags=re.IGNORECASE)
        # Remove tool call tags
        clean = re.sub(r"<tool_call>[\s\S]*?</tool_call>", "", clean, flags=re.IGNORECASE)

        # Replace markdown code blocks with verbal cue
        clean = re.sub(r"```[\s\S]*?```", " [code omitted] ", clean)

        # Remove inline code backticks
        clean = re.sub(r"`([^`]+)`", r"\1", clean)

        # Remove markdown links [text](url) -> text
        clean = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", clean)

        # Remove markdown headers (# Header)
        clean = re.sub(r"^#{1,6}\s+", "", clean, flags=re.MULTILINE)

        # Remove bold / italic markdown markers (*text*, **text**, _text_)
        clean = re.sub(r"[*_]{1,3}([^*_]+)[*_]{1,3}", r"\1", clean)

        # Remove blockquotes (> quote)
        clean = re.sub(r"^>\s+", "", clean, flags=re.MULTILINE)

        # Strip conversational tool announcements (e.g. "use the write_file function: { ... }")
        clean = re.sub(
            r"(?:use(?: the)?|execute(?: the)?|call(?: the)?|invok(?:e|ing)(?: the)?)\s+`?[a-z_]+`?(?:\s+tool|\s+function)?[\s\S]*?\{[\s\S]*?\}",
            "",
            clean,
            flags=re.IGNORECASE
        )

        # Remove stray JSON objects / arrays
        clean = re.sub(r"\{[^{}]*:[^{}]*\}", "", clean)

        # Remove emojis and bullet icons
        clean = re.sub(r"[•⚡🎯📝⚠️📊🔍💬✓\*\-]", " ", clean)

        # Collapse whitespace
        clean = re.sub(r"\s+", " ", clean).strip()

        return clean

    def chunk_text(self, text: str, max_chars: Optional[int] = None) -> list[str]:
        """
        Splits long responses on sentence boundaries (. , ! , ? , \n) to respect
        neural TTS input length ceilings and prevent synthesis quality degradation.
        """
        limit = max_chars or self.chunk_size_chars
        clean = self.sanitize_text(text)
        if not clean:
            return []

        if len(clean) <= limit:
            return [clean]

        # Split into raw sentences using punctuation boundaries
        sentence_delimiters = re.compile(r"(?<=[.!?\n])\s+")
        raw_sentences = [s.strip() for s in sentence_delimiters.split(clean) if s.strip()]

        chunks: list[str] = []
        current_chunk = ""

        for sent in raw_sentences:
            # If a single sentence exceeds the limit, chunk it by commas/semicolons or words
            if len(sent) > limit:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                    current_chunk = ""

                sub_parts = re.split(r"(?<=[,;])\s+", sent)
                for part in sub_parts:
                    if len(part) > limit:
                        words = part.split(" ")
                        temp = ""
                        for w in words:
                            if len(temp) + len(w) + 1 <= limit:
                                temp = f"{temp} {w}".strip()
                            else:
                                if temp:
                                    chunks.append(temp)
                                temp = w
                        if temp:
                            chunks.append(temp)
                    else:
                        if len(current_chunk) + len(part) + 1 <= limit:
                            current_chunk = f"{current_chunk} {part}".strip()
                        else:
                            if current_chunk:
                                chunks.append(current_chunk.strip())
                            current_chunk = part
                continue

            if len(current_chunk) + len(sent) + 1 <= limit:
                current_chunk = f"{current_chunk} {sent}".strip()
            else:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                current_chunk = sent

        if current_chunk:
            chunks.append(current_chunk.strip())

        return chunks

    # --- 3. Speech Synthesis Pipeline ---

    def synthesize(self, text: str, voice_id: Optional[str] = None) -> bytes:
        """
        Synthesizes spoken text into raw WAV audio bytes through the local fallback cascade:
        1. Chatterbox TTS (primary)
        2. Kokoro TTS (local VRAM fallback)
        3. Text-only response (returns b"") with logged reason.
        Strictly local — zero network/cloud egress.
        """
        chunks = self.chunk_text(text)
        if not chunks:
            logger.debug("Synthesize called with empty or filtered text.")
            return b""

        # Attempt Tier 1: Chatterbox
        if not self.is_chatterbox_loaded():
            loaded = self.load_model("chatterbox")
            if not loaded:
                logger.info("Chatterbox unavailable. Attempting Kokoro fallback...")
                return self._synthesize_kokoro(chunks, voice_id)

        try:
            return self._synthesize_chatterbox(chunks, voice_id)
        except Exception as e:
            logger.warning("Chatterbox synthesis error: %s. Falling back to Kokoro...", e)
            return self._synthesize_kokoro(chunks, voice_id)

    def _synthesize_chatterbox(self, chunks: list[str], voice_id: Optional[str] = None) -> bytes:
        """Runs Chatterbox synthesis over chunks and concatenates audio waveforms."""
        import soundfile as sf

        act_id = None
        if self.governor:
            act_id = self.governor.begin_activity(ActivityType.TTS_INFERENCE, label="chatterbox_synthesis")

        try:
            audio_segments = []
            sample_rate = getattr(self._chatterbox_model, "sr", 24000)

            for chunk in chunks:
                if not chunk:
                    continue
                kwargs = {}
                if voice_id:
                    kwargs["audio_prompt_path"] = voice_id
                wav = self._chatterbox_model.generate(chunk, **kwargs)

                # Convert torch tensor / numpy array to standard numpy float32
                if hasattr(wav, "detach"):
                    wav_np = wav.detach().cpu().numpy()
                else:
                    wav_np = np.asarray(wav)

                if wav_np.ndim > 1:
                    wav_np = wav_np.squeeze()

                audio_segments.append(wav_np)

            if not audio_segments:
                return b""

            combined = np.concatenate(audio_segments) if len(audio_segments) > 1 else audio_segments[0]

            buffer = io.BytesIO()
            sf.write(buffer, combined, sample_rate, format="WAV")
            return buffer.getvalue()

        finally:
            if self.governor and act_id:
                self.governor.end_activity(act_id)

    def _synthesize_kokoro(self, chunks: list[str], voice_id: Optional[str] = None) -> bytes:
        """Tier 2 fallback: Synthesizes via Kokoro TTS."""
        if not self.is_kokoro_loaded():
            loaded = self.load_model("kokoro")
            if not loaded:
                logger.warning(
                    "TTS fallback gate exhausted: Neither Chatterbox nor Kokoro can run under current VRAM conditions. "
                    "Falling back to text-only output (0 cloud egress)."
                )
                return b""

        import soundfile as sf

        act_id = None
        if self.governor:
            act_id = self.governor.begin_activity(ActivityType.TTS_INFERENCE, label="kokoro_synthesis")

        try:
            audio_segments = []
            sample_rate = 24000
            voice = voice_id or "af_heart"

            for chunk in chunks:
                if not chunk:
                    continue
                if hasattr(self._kokoro_model, "create"):
                    samples, sr = self._kokoro_model.create(chunk, voice=voice, speed=1.0, lang="en-us")
                    sample_rate = sr
                elif callable(self._kokoro_model):
                    samples = self._kokoro_model(chunk, voice=voice)
                else:
                    samples = None

                if samples is not None:
                    arr = np.asarray(samples, dtype=np.float32)
                    if arr.ndim > 1:
                        arr = arr.squeeze()
                    audio_segments.append(arr)

            if not audio_segments:
                return b""

            combined = np.concatenate(audio_segments) if len(audio_segments) > 1 else audio_segments[0]
            buffer = io.BytesIO()
            sf.write(buffer, combined, sample_rate, format="WAV")
            return buffer.getvalue()

        except Exception as e:
            logger.error(
                "Kokoro synthesis failed: %s. Falling back to text-only output (0 cloud egress).",
                e
            )
            return b""
        finally:
            if self.governor and act_id:
                self.governor.end_activity(act_id)
