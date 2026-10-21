"""Pairing a phone or iPad with Jarvis (PLAN 4.19); see ``app.remote`` for how remote is decided.

Desktop-only (the remote port answers 404): ``GET/POST /api/remote/pairing`` (the code, its QR and
the address the phone opens), ``GET /api/remote/devices``, ``DELETE /api/remote/devices/{id}``.
From the phone: ``POST /api/remote/pair`` (no token yet), ``GET /api/remote/me``, and its voice:
``POST /api/remote/transcribe`` (a recorded message to words) and ``POST /api/remote/say`` (a reply to
audio).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, File, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, Field

from app.config import settings
from app.remote import get_remote_devices, is_remote_request, remote_device

logger = logging.getLogger("jarvis.routers.remote")

router = APIRouter(prefix="/api/remote", tags=["remote"])

_TAILSCALE_CANDIDATES = (r"C:\Program Files\Tailscale\tailscale.exe", r"D:\Tailscale\tailscale.exe")


def _tailscale_exe() -> Optional[str]:
    found = shutil.which("tailscale")
    if found:
        return found
    return next((p for p in _TAILSCALE_CANDIDATES if Path(p).is_file()), None)


def _tailscale_json(*args: str) -> Optional[dict[str, Any]]:
    exe = _tailscale_exe()
    if not exe:
        return None
    try:
        out = subprocess.run([exe, *args, "--json"], capture_output=True, text=True, timeout=5,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return json.loads(out.stdout) if out.returncode == 0 and out.stdout.strip() else None
    except Exception as exc:
        logger.info("tailscale %s failed: %s", " ".join(args), exc)
        return None


def tailscale_state() -> dict[str, Any]:
    """What Settings needs to say whether the phone can reach Jarvis: Tailscale installed and
    running, this PC's tailnet name, and whether ``tailscale serve`` forwards to the remote port."""
    exe = _tailscale_exe()
    if not exe:
        return {"installed": False, "running": False, "host": None, "serving": False}
    st = _tailscale_json("status") or {}
    host = str((st.get("Self") or {}).get("DNSName") or "").rstrip(".") or None
    running = st.get("BackendState") == "Running"
    serve = _tailscale_json("serve", "status") or {}
    target = f"127.0.0.1:{settings.remote_port}"
    serving = target in json.dumps(serve) or f"localhost:{settings.remote_port}" in json.dumps(serve)
    return {"installed": True, "running": running, "host": host, "serving": serving,
            "serve_command": f"tailscale serve --bg http://127.0.0.1:{settings.remote_port}"}


def _qr_svg(text: str) -> str:
    import segno

    # Black on white with the quiet zone, whatever the app's theme: cameras need the contrast.
    return segno.make(text, error="m").svg_inline(scale=6, border=4, dark="#000000", light="#ffffff")


def _pairing_payload(code: dict[str, Any]) -> dict[str, Any]:
    ts = tailscale_state()
    url = f"https://{ts['host']}/m/#pair={code['code']}" if ts.get("host") else None
    return {**code, "url": url, "qr_svg": _qr_svg(url) if url else None, "tailscale": ts}


def _desktop_only(request: Request) -> None:
    if is_remote_request(request):  # the middleware already 404s these; belt and braces
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not available remotely.")


@router.get("/pairing")
def get_pairing(request: Request) -> dict[str, Any]:
    """The live pairing code (a new one when none is live), with its QR and the phone's address."""
    _desktop_only(request)
    devices = get_remote_devices()
    return _pairing_payload(devices.current_code() or devices.new_pairing_code())


@router.post("/pairing")
def new_pairing(request: Request) -> dict[str, Any]:
    """Settings' New code button: the old code stops working."""
    _desktop_only(request)
    return _pairing_payload(get_remote_devices().new_pairing_code())


@router.get("/devices")
def list_devices(request: Request) -> dict[str, Any]:
    _desktop_only(request)
    return {"devices": get_remote_devices().list(), "tailscale": tailscale_state()}


@router.delete("/devices/{device_id}")
def remove_device(device_id: str, request: Request) -> dict[str, Any]:
    _desktop_only(request)
    if not get_remote_devices().remove(device_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No paired device {device_id}.")
    return {"removed": device_id}


class PairRequest(BaseModel):
    code: str = Field(..., min_length=1, max_length=32)
    name: str = Field(default="", max_length=80)


@router.post("/pair")
def pair(body: PairRequest) -> dict[str, Any]:
    """The phone's pairing screen (or its QR link): a live code buys a device token, shown once."""
    result = get_remote_devices().pair(body.code, body.name)
    if result is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="That code didn't work. Codes last 10 minutes.")
    return result


_QUANT_TAIL = re.compile(r"[.\-_](?:i1-)?(?:UD-)?(?:I?Q\d|BF16|F16|F32|pq\d|ptq\d)", re.IGNORECASE)


def short_model_name(file_name: Optional[str]) -> Optional[str]:
    """``Ornith-1.5-9B.i1-IQ3_XXS.gguf`` -> ``Ornith 1.5 9B``: the pill's name, quant left off."""
    if not file_name:
        return None
    stem = file_name[:-5] if file_name.lower().endswith(".gguf") else file_name
    return _QUANT_TAIL.split(stem, maxsplit=1)[0].replace("-", " ").replace("_", " ").strip() or stem


@router.get("/status")
async def remote_status() -> dict[str, Any]:
    """Everything the phone's status pill and sheet show, in one call: the model, VRAM and GPU,
    the Governor switch and Game mode (the desktop reads the same from /api/models,
    /governor/status and /health, each heavier than a phone needs every few seconds)."""
    from app.agent.runtime_process_manager import get_runtime_process_manager
    from app.main import game_mode, governor

    manager = get_runtime_process_manager()
    metrics = await governor.get_metrics()
    served = manager.served_model.name if manager.served_model else None
    return {
        "model": short_model_name(served),
        "model_file": served,
        "slot": manager.current_model_kind,
        "ctx_size": manager.served_ctx_size,
        "launch_adjustment": manager.launch_adjustment,
        "model_unloaded": governor.model_unloaded,
        "governor_status": governor.status.value,
        "governor_auto": not governor.switched_off,
        "game_mode": game_mode.active,
        "vram_used_mb": metrics.vram_used_mb,
        "vram_total_mb": metrics.vram_total_mb,
        "model_vram_mb": metrics.model_vram_mb,
        "gpu_util_percent": metrics.gpu_util_percent,
        "gpu_temp_c": metrics.gpu_temp_c,
        "vram_threshold": governor.vram_threshold,
        "gpu_threshold": governor.gpu_threshold,
    }


# ---- notifications (PLAN 4.19 phase 2, app/remote_push.py) --------------------------------------

def _this_device(request: Request) -> dict[str, Any]:
    device = remote_device(request)
    if device is None:  # the desktop has no subscription to keep
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only a paired phone or tablet can do this.")
    return device


@router.get("/push/key")
def push_key() -> dict[str, Any]:
    """The key the phone subscribes with (made on first use)."""
    from app.remote_push import public_key

    return {"public_key": public_key()}


class PushSubscription(BaseModel):
    endpoint: str = Field(..., min_length=10, max_length=2048)
    keys: dict[str, str]
    expirationTime: Optional[float] = None


@router.post("/push")
def push_subscribe(body: PushSubscription, request: Request) -> dict[str, Any]:
    """The phone's Notifications switch turned on: keep its subscription."""
    device = _this_device(request)
    if not body.endpoint.startswith("https://") or not {"p256dh", "auth"} <= set(body.keys):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Not a web-push subscription.")
    get_remote_devices().set_push(device["id"], {"endpoint": body.endpoint, "keys": body.keys})
    return {"notifications": True}


@router.delete("/push")
def push_unsubscribe(request: Request) -> dict[str, Any]:
    get_remote_devices().set_push(_this_device(request)["id"], None)
    return {"notifications": False}


@router.post("/push/test")
def push_test(request: Request) -> dict[str, Any]:
    """Sends this device a test notification now (away from the PC or not)."""
    from app.remote_push import RemotePusher

    result = RemotePusher(get_remote_devices).test(_this_device(request)["id"])
    if result is None:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="The push service couldn't be reached.")
    if result not in (200, 201, 202):
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"The push service refused it ({result}).")
    return {"sent": True}


# ---- voice from the phone (PLAN 4.19 phase 3) -----------------------------------------------------

_MAX_CLIP_BYTES = 8 * 1024 * 1024  # minutes of Opus/AAC; a spoken message is seconds
_CLIP_FORMATS = {"webm", "ogg", "mp4", "m4a", "aac", "wav", "mp3"}


def _clip_format(file: UploadFile) -> str:
    ext = (file.filename or "").rsplit(".", 1)[-1].lower()
    if ext in _CLIP_FORMATS:
        return ext
    sub = (file.content_type or "").split(";")[0].split("/")[-1].lower()  # "audio/mp4;codecs=..." -> mp4
    return sub if sub in _CLIP_FORMATS else "webm"


@router.post("/transcribe")
async def transcribe(file: UploadFile = File(...)) -> dict[str, Any]:
    """The phone's mic: one whole recorded message to text, with the PC's Whisper. Only the words
    come back; the phone sends them as an ordinary remote chat turn, so the stricter gate applies
    and no wake word, hands-free state or voice-mode routing is involved -- that machinery is the
    desktop's own (/api/voice/listen), and a phone message must not move it."""
    from app.routers.voice import get_transcriber

    data = await file.read(_MAX_CLIP_BYTES + 1)
    if not data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty recording.")
    if len(data) > _MAX_CLIP_BYTES:
        raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="Recording too long.")
    # Whisper is synchronous and takes a second or more: off the event loop, so the phone's own
    # stream and the desktop keep moving meanwhile.
    result = await asyncio.to_thread(get_transcriber().transcribe_audio_bytes, data, _clip_format(file))
    return {
        "text": (result.get("text") or "").strip(),
        "duration_seconds": result.get("duration_seconds") or 0.0,
        "error": result.get("error"),
    }


class SayRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=8000)


@router.post("/say")
async def say(body: SayRequest) -> dict[str, Any]:
    """A reply as the persona would say it, in its voice, made on the PC (the phone has no Kokoro):
    the spoken text and MP3 audio. Unlike /api/voice/say it leaves the desktop's voice state alone."""
    from app.persona import persona_manager
    from app.routers.voice import get_synthesizer

    persona = persona_manager.get_active()
    spoken = persona.shape_for_speech(body.text)
    if not spoken:
        return {"spoken_text": "", "audio_base64": "", "audio_mime": "audio/mpeg"}
    audio = await get_synthesizer().generate_neural_audio_bytes(spoken, voice=persona.voice_id)
    return {
        "spoken_text": spoken,
        "audio_base64": base64.b64encode(audio).decode("ascii") if audio else "",
        "audio_mime": "audio/mpeg",
    }


@router.get("/me")
def me(request: Request) -> dict[str, Any]:
    """Which paired device this is (the phone checks its token is still good on start)."""
    return {"remote": is_remote_request(request), "device": remote_device(request)}
