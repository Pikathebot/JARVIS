import asyncio
import base64
import logging
from typing import Any, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field

from app.persona import persona_manager
from app.voice.session import VoiceSessionManager, VoiceState
from app.voice.synthesizer import AVAILABLE_NEURAL_VOICES, VoiceSynthesizer
from app.voice.transcriber import AudioTranscriber
from app.voice.voice_mode import get_voice_mode

logger = logging.getLogger("jarvis.routers.voice")

router = APIRouter(prefix="/api/voice", tags=["voice"])

# One hands-free state machine for the process; the desktop client drives it.
voice_sessions = VoiceSessionManager()


def get_transcriber() -> AudioTranscriber:
    try:
        from app.main import transcriber

        return transcriber
    except Exception:
        return AudioTranscriber()


def get_synthesizer() -> VoiceSynthesizer:
    try:
        from app.main import synthesizer

        return synthesizer
    except Exception:
        return VoiceSynthesizer()


class SayRequest(BaseModel):
    text: str = Field(..., description="Screen reply to convert into spoken audio")
    session_id: str = Field(default="default")
    voice_id: Optional[str] = Field(
        default=None, description="Override the persona's voice for this utterance"
    )
    shape: bool = Field(
        default=True,
        description="Apply the persona's speech shaping (strip markdown, cap length)",
    )


class VoiceModeRequest(BaseModel):
    enabled: bool


class VoiceCommandRequest(BaseModel):
    text: str
    session_id: str = Field(default="default")


class VoiceStateRequest(BaseModel):
    state: str = Field(..., description="idle | listening | armed | thinking | speaking")


# ------------------------------------------------------------------ listening


@router.post("/listen", response_model=dict)
async def listen(
    file: UploadFile = File(..., description="Captured audio chunk (webm/wav/mp3)"),
    session_id: str = Form(default="default"),
) -> dict[str, Any]:
    """
    Transcribe one captured chunk and decide whether it was addressed to Jarvis.

    The client streams short chunks here continuously while hands-free mode is
    on; only chunks that carry a wake word (or land inside the follow-up
    window) come back with should_respond set.
    """
    data = await file.read()
    if not data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Empty audio upload."
        )

    fmt = (file.filename or "audio.webm").rsplit(".", 1)[-1].lower()
    # On a worker thread: Bark's first start blocks 8-30 s and would freeze every other request.
    result = await asyncio.to_thread(get_transcriber().transcribe_audio_bytes, data, format=fmt)
    transcript = (result.get("text") or "").strip()

    # transcribe_audio_bytes strips wake-word prefixes for convenience; the
    # decision needs the untouched text to tell "Jarvis, do X" from "do X".
    raw_transcript = (result.get("raw_text") or transcript).strip()

    persona = persona_manager.get_active()
    decision = voice_sessions.evaluate_utterance(
        session_id=session_id,
        transcript=raw_transcript,
        greeting=persona.greeting,
        spoken_seconds=float(result.get("duration_seconds") or 0.0),
    )

    payload = decision.to_dict()
    payload["voice_route"] = None
    payload["voice_plans"] = []
    mode = get_voice_mode()
    if mode.enabled and decision.should_respond and decision.query:
        # Voice mode: Laya decides. A direct action runs here and comes back as a line to say
        # (should_respond false, nothing for the chat to do); talk, questions and doubt go back
        # to the client as a chat turn carrying voice_route for MiniCPM.
        outcome = await mode.handle(decision.query, session_id)
        payload["voice_plans"] = outcome.plans
        if outcome.route:
            payload["query"], payload["voice_route"] = outcome.query, outcome.route
        else:
            payload["should_respond"] = False
            payload["speak_immediately"] = outcome.spoken
            payload["reason"] = "voice_mode_action"
            if outcome.awaiting_confirmation:
                voice_sessions.arm_follow_up(session_id)  # the yes/no needs no wake word
    payload["transcription_error"] = result.get("error")
    payload["duration_seconds"] = result.get("duration_seconds", 0.0)
    payload["session"] = voice_sessions.get(session_id).to_dict()
    return payload


@router.post("/say", response_model=dict)
async def say(req: SayRequest) -> dict[str, Any]:
    """
    Render a reply as the persona would actually say it, plus MP3 audio.

    Audio is returned base64-encoded alongside the spoken text so the client
    can show what is being said while it plays.
    """
    persona = persona_manager.get_active()
    spoken_text = persona.shape_for_speech(req.text) if req.shape else req.text

    if not spoken_text:
        return {
            "spoken_text": "",
            "audio_base64": "",
            "voice_id": req.voice_id or persona.voice_id,
            "session": voice_sessions.get(req.session_id).to_dict(),
        }

    voice_id = req.voice_id or persona.voice_id
    audio = await get_synthesizer().generate_neural_audio_bytes(spoken_text, voice=voice_id)

    voice_sessions.set_state(req.session_id, VoiceState.SPEAKING)

    return {
        "spoken_text": spoken_text,
        "audio_base64": base64.b64encode(audio).decode("ascii") if audio else "",
        "audio_mime": "audio/mpeg",
        "voice_id": voice_id,
        "persona_id": persona_manager.active_id,
        "synthesis_failed": not audio,
        "session": voice_sessions.get(req.session_id).to_dict(),
    }


# -------------------------------------------------------------- session state


@router.get("/session/{session_id}", response_model=dict)
async def get_voice_session(session_id: str) -> dict[str, Any]:
    """Current hands-free state for one chat session."""
    return voice_sessions.get(session_id).to_dict()


@router.post("/session/{session_id}/start", response_model=dict)
async def start_hands_free(session_id: str) -> dict[str, Any]:
    """Begin listening for the wake word."""
    return voice_sessions.start_listening(session_id).to_dict()


@router.post("/session/{session_id}/stop", response_model=dict)
async def stop_hands_free(session_id: str) -> dict[str, Any]:
    """Stop listening and drop any follow-up arming."""
    return voice_sessions.stop_listening(session_id).to_dict()


@router.post("/session/{session_id}/state", response_model=dict)
async def set_voice_state(session_id: str, req: VoiceStateRequest) -> dict[str, Any]:
    """
    Report client-side state transitions (notably barge-in: the client moves
    back to 'listening' the moment the user talks over a spoken reply).
    """
    try:
        state = VoiceState(req.state.lower())
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown voice state '{req.state}'. Expected one of: "
            + ", ".join(s.value for s in VoiceState),
        )
    return voice_sessions.set_state(session_id, state).to_dict()


@router.post("/session/{session_id}/arm", response_model=dict)
async def arm_follow_up(session_id: str) -> dict[str, Any]:
    """
    Arm the follow-up window after a reply, so the next question needs no
    wake word.
    """
    return voice_sessions.arm_follow_up(session_id).to_dict()


@router.get("/mode", response_model=dict)
async def voice_mode_status() -> dict[str, Any]:
    """Voice mode (PLAN 4.9): on/off, a swap in progress, the loaded model, the Laya sidecar."""
    return get_voice_mode().status()


@router.post("/mode", response_model=dict)
async def set_voice_mode(req: VoiceModeRequest) -> dict[str, Any]:
    """Start turning voice mode on (main model out, MiniCPM + Laya + GPU Whisper in) or off (back
    to main). Returns at once with ``busy`` set; poll GET /mode until ``busy`` is empty."""
    return get_voice_mode().start(req.enabled)


@router.post("/command", response_model=dict)
async def voice_command(req: VoiceCommandRequest) -> dict[str, Any]:
    """A typed utterance through voice mode's brain, exactly as a spoken one after the wake word
    -- for testing commands without the microphone. Voice mode must be on."""
    mode = get_voice_mode()
    if not mode.enabled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Voice mode is off.")
    return (await mode.handle(req.text, req.session_id)).to_dict()


@router.get("/hands-free", response_model=dict)
async def hands_free_status() -> dict[str, Any]:
    """Wake words, follow-up window, active voice sessions, and the persona voice."""
    persona = persona_manager.get_active()
    payload = voice_sessions.status()
    payload["persona_id"] = persona_manager.active_id
    payload["voice_id"] = persona.voice_id
    payload["greeting"] = persona.greeting
    payload["available_voices"] = AVAILABLE_NEURAL_VOICES
    return payload
