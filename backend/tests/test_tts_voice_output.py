import io
import time
import pytest
import httpx
from unittest.mock import MagicMock, patch, AsyncMock
import numpy as np
import soundfile as sf

from app.main import app
from app.agent.tts.chatterbox_engine import ChatterboxEngine
from app.agent.tools.audio_playback import play_audio, stop_playback, is_playing
from app.agent.tools.registry import AVAILABLE_TOOLS, TOOL_SCHEMAS, execute_tool
from app.agent.permissions import BASE_TOOL_RISK_MAP, RiskTier
from app.agent.orchestrator import AgentOrchestrator, OrchestratorResult
from app.governor.resource_governor import ResourceGovernor, ActivityType, SystemMetrics
from app.config import settings


# --- 1. Audio Playback & Cancellation Tests ---

def test_audio_playback_and_interruption():
    """Verify non-blocking playback and instant stop_playback interruption."""
    sr = 22050
    t = np.linspace(0, 0.5, int(sr * 0.5), endpoint=False)
    sig = 0.2 * np.sin(2 * np.pi * 440 * t)
    buf = io.BytesIO()
    sf.write(buf, sig, sr, format="WAV")
    dummy_wav = buf.getvalue()

    # Empty payload handling
    assert play_audio(b"") == "No audio bytes provided for playback."

    with patch("sounddevice.play") as mock_sd_play, \
         patch("sounddevice.wait") as mock_sd_wait, \
         patch("sounddevice.stop") as mock_sd_stop:

        res = play_audio(dummy_wav)
        assert "Audio playback started" in res

        stop_res = stop_playback()
        assert stop_res == "Audio playback stopped."
        mock_sd_stop.assert_called()


# --- 2. Permission Tier & Tool Registry Tests ---

def test_tts_tool_registration_and_permissions():
    """stop_playback is offered to the model; play_audio is registered for the backend's own use
    but never offered -- it takes raw audio bytes, which no model can produce."""
    from app.agent.tools.registry import TOOL_FUNCTIONS
    tool_names = [getattr(t, "__name__", str(t)) for t in AVAILABLE_TOOLS]
    assert "play_audio" not in tool_names
    assert "play_audio" in TOOL_FUNCTIONS
    assert "stop_playback" in tool_names

    assert "play_audio" in TOOL_SCHEMAS
    assert "stop_playback" in TOOL_SCHEMAS

    assert BASE_TOOL_RISK_MAP["play_audio"] == RiskTier.LOW_RISK
    assert BASE_TOOL_RISK_MAP["stop_playback"] == RiskTier.LOW_RISK


# --- 3. Text Sanitization & Sentence Chunking Tests ---

def test_text_sanitization():
    """Verify markdown, tool JSON, and code blocks are properly sanitized for speech."""
    engine = ChatterboxEngine()

    raw_text = (
        "# System Status Update\n\n"
        "Here is the report with **bold details** and *italics* and a [link](https://example.com).\n"
        "```python\nimport os\nprint('hello world')\n```\n"
        "Use the write_file function: {\"file_path\": \"test.txt\", \"content\": \"data\"}\n"
        "Everything is running smoothly! <thought>Internal monologue</thought>"
    )

    clean = engine.sanitize_text(raw_text)

    assert "#" not in clean
    assert "**" not in clean
    assert "https://example.com" not in clean
    assert "link" in clean
    assert "[code omitted]" in clean
    assert "write_file" not in clean
    assert '{"file_path"' not in clean
    assert "Internal monologue" not in clean
    assert "Everything is running smoothly!" in clean


def test_chunk_text_long_input():
    """Verify sentence-boundary chunking handles >1000 character inputs cleanly."""
    engine = ChatterboxEngine(chunk_size_chars=200)

    sentences = [
        "First sentence discussing architectural principles of local AI systems.",
        "Second sentence covering memory compaction and rolling context limits.",
        "Third sentence describing the governor resource hysteresis subsystem in depth.",
        "Fourth sentence validating non-blocking audio streams and prompt interruption.",
        "Fifth sentence emphasizing zero network egress for speech synthesis privacy.",
        "Sixth sentence confirming seamless fallback from Chatterbox to Kokoro and text-only.",
        "Seventh sentence verifying performance telemetry on RTX 4060 hardware."
    ]
    long_input = " ".join(sentences * 3)
    assert len(long_input) > 1000

    chunks = engine.chunk_text(long_input, max_chars=200)
    assert len(chunks) > 5

    for c in chunks:
        assert len(c) <= 250
        assert len(c) > 0


# --- 4. Governor V2 Integration & VRAM Gate Tests ---

def test_governor_tts_activity_and_vram_gate():
    """Verify Governor can_allocate_vram gate checks and ActivityType.TTS_INFERENCE."""
    assert ActivityType.TTS_INFERENCE.value == "tts_inference"

    gov = ResourceGovernor(enabled=True)

    # Mock metrics with sufficient VRAM
    good_metrics = SystemMetrics(
        gpu_available=True,
        vram_total_mb=8188.0,
        vram_used_mb=2000.0,
        vram_free_mb=6188.0
    )
    with patch.object(gov, "collect_metrics", return_value=good_metrics):
        can_alloc, reason = gov.can_allocate_vram(2500.0)
        assert can_alloc is True
        assert "headroom verified" in reason

    # Mock metrics with saturated VRAM
    tight_metrics = SystemMetrics(
        gpu_available=True,
        vram_total_mb=8188.0,
        vram_used_mb=7500.0,
        vram_free_mb=688.0
    )
    with patch.object(gov, "collect_metrics", return_value=tight_metrics):
        can_alloc, reason = gov.can_allocate_vram(2500.0)
        assert can_alloc is False
        assert "Insufficient physical VRAM" in reason or "exceeds threshold" in reason


# --- 5. Chatterbox -> Kokoro -> Text-Only Local Fallback Tests ---

def test_chatterbox_to_kokoro_to_text_fallback():
    """Verify strict local fallback cascade (Chatterbox -> Kokoro -> text-only)."""
    gov = ResourceGovernor(enabled=True)
    engine = ChatterboxEngine(governor=gov, vram_required_mb=2500.0, kokoro_vram_required_mb=1000.0)

    # 1. When Chatterbox succeeds
    dummy_wav_array = np.zeros(2400, dtype=np.float32)
    mock_chatterbox_model = MagicMock()
    mock_chatterbox_model.generate.return_value = dummy_wav_array
    mock_chatterbox_model.sr = 24000

    with patch.object(gov, "can_allocate_vram", return_value=(True, "OK")):
        engine._chatterbox_model = mock_chatterbox_model
        audio = engine.synthesize("Hello Jarvis.")
        assert len(audio) > 0
        assert engine.is_loaded() is True

    # 2. When Chatterbox VRAM gate rejects, fallback to Kokoro
    engine.unload_model()
    assert engine.is_loaded() is False

    mock_kokoro_model = MagicMock()
    mock_kokoro_model.create.return_value = (dummy_wav_array, 24000)

    def mock_gate(req_mb, **kwargs):
        if req_mb >= 2000:
            return (False, "Chatterbox VRAM exceeded")
        return (True, "Kokoro VRAM OK")

    with patch.object(gov, "can_allocate_vram", side_effect=mock_gate), \
         patch.object(engine, "load_model", side_effect=lambda b: setattr(engine, "_kokoro_model", mock_kokoro_model) or True if b == "kokoro" else False):
        audio = engine.synthesize("Fallback to kokoro.")
        assert len(audio) > 0
        assert engine.is_kokoro_loaded() is True

    # 3. When both Chatterbox and Kokoro are rejected, falls back to text-only (b"")
    engine.unload_model()
    with patch.object(gov, "can_allocate_vram", return_value=(False, "Host fully saturated")):
        audio = engine.synthesize("This will be text only.")
        assert audio == b""


# --- 6. Orchestrator Integration & Conversational Toggle Tests ---

@pytest.mark.anyio
async def test_orchestrator_voice_interruption_and_toggle():
    """Verify turn-start audio interruption, voice commands, and speech routing."""
    mock_engine = MagicMock()
    mock_engine.sanitize_text = lambda x: x
    mock_engine.synthesize.return_value = b"fake_wav_bytes"

    orch = AgentOrchestrator(
        tts_engine=mock_engine,
        voice_output_enabled=False
    )

    with patch("app.agent.orchestrator.stop_playback") as mock_stop, \
         patch("app.agent.orchestrator.play_audio") as mock_play, \
         patch.object(orch, "_run_internal", new_callable=AsyncMock) as mock_internal:

        mock_internal.return_value = OrchestratorResult(
            response="Hello there!",
            model="hermes3:8b",
            status="completed"
        )

        # Turn starts: stop_playback is always called
        res = await orch.run("Hello", session_id="test_session")
        mock_stop.assert_called()

        # Conversational commands: 'voice on'
        res_on = await orch.run("voice on", session_id="test_session")
        assert "Voice output is now enabled" in res_on.response
        assert orch.voice_output_enabled is True
        mock_play.assert_called_once()

        # Conversational commands: 'stop talking'
        res_stop = await orch.run("stop talking", session_id="test_session")
        assert "stopped speaking" in res_stop.response

        # Conversational commands: 'voice off'
        res_off = await orch.run("voice off", session_id="test_session")
        assert "Voice output is now disabled" in res_off.response
        assert orch.voice_output_enabled is False


@pytest.mark.anyio
async def test_orchestrator_excludes_tool_call_json_from_speech():
    """Verify tool JSON is never spoken by the voice pipeline."""
    mock_engine = MagicMock()
    mock_engine.sanitize_text.side_effect = lambda t: ChatterboxEngine().sanitize_text(t)
    mock_engine.synthesize.return_value = b"synthesized_audio"

    orch = AgentOrchestrator(
        tts_engine=mock_engine,
        voice_output_enabled=True
    )

    with patch.object(orch, "_run_internal") as mock_run_int, \
         patch("app.agent.orchestrator.play_audio") as mock_play:

        response_with_tool_call = (
            "Here is your file content.\n"
            "<tool_call>{\"name\": \"read_file\", \"arguments\": {\"file_path\": \"test.py\"}}</tool_call>\n"
            "Hope this helps!"
        )
        mock_run_int.return_value = OrchestratorResult(
            response=response_with_tool_call,
            model="hermes3:8b",
            status="completed"
        )

        res = await orch.run("Check test.py", session_id="test_tool_turn")
        assert res.status == "completed"

        # Verify synthesize was called with clean natural language, not tool call JSON
        called_args = mock_engine.synthesize.call_args[0][0]
        assert "read_file" not in called_args
        assert "{" not in called_args
        assert "Here is your file content" in called_args
        assert "Hope this helps!" in called_args
        mock_play.assert_called_once_with(b"synthesized_audio")


# --- 7. Voice Output API Endpoints Tests ---

@pytest.mark.anyio
async def test_voice_output_api_endpoints():
    """Verify /api/voice/output GET and POST endpoints."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ac:
        # 1. GET status
        res_get = await ac.get("/api/voice/output")
        assert res_get.status_code == 200
        data_get = res_get.json()
        assert "enabled" in data_get
        assert "engine" in data_get
        assert "is_loaded" in data_get
        assert "is_playing" in data_get

        # 2. POST toggle on
        res_post_on = await ac.post("/api/voice/output", json={"enabled": True})
        assert res_post_on.status_code == 200
        assert res_post_on.json()["enabled"] is True
        assert settings.voice_output_enabled is True

        # 3. POST toggle off
        res_post_off = await ac.post("/api/voice/output", json={"enabled": False})
        assert res_post_off.status_code == 200
        assert res_post_off.json()["enabled"] is False
        assert settings.voice_output_enabled is False


def test_cpu_tts_load_skips_the_vram_gate():
    """A CPU-resident TTS engine takes system RAM, so the governor's VRAM gate must not veto it;
    a CUDA load is still gated."""
    from unittest.mock import MagicMock
    from app.agent.tts.chatterbox_engine import ChatterboxEngine

    gov = MagicMock()
    gov.can_allocate_vram.return_value = (False, "VRAM tight")

    cpu = ChatterboxEngine(governor=gov, device="cpu")
    assert cpu.load_model("chatterbox") is False  # the package is not installed here...
    gov.can_allocate_vram.assert_not_called()      # ...but the gate was never the reason

    cuda = ChatterboxEngine(governor=gov, device="cuda")
    assert cuda.load_model("chatterbox") is False
    gov.can_allocate_vram.assert_called_once_with(cuda.vram_required_mb)
