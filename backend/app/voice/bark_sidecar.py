"""
Bark sidecar: the main speech-to-text engine (Underdog Bark 3.0 Pro), a torch process.

Like Laya it lives in ``tools/laya-venv`` so the backend's venv stays small; this starts
``scripts/bark_server.py`` there and sends it 16 kHz mono float32 samples over loopback HTTP.
It runs on the CPU (~0.8 s for a 6 s clip) and moves to CUDA bf16 (~0.4 s, ~1.3 GB) while voice
mode is on -- ``AudioTranscriber.use_gpu`` asks for that. Its VRAM is reported through ``vram_mb``
so the governor counts it as ours.

On the CPU it holds ~3.4 GB of RAM (fp32 weights + torch; ~7 GB committed), measured 2026-10-23 --
bf16 on this CPU is 35-50x slower and int8 dynamic quantisation larger, so the size stays. Hence it
is never waited for: an utterance that finds it not ready starts it in the background and Whisper
answers (a cold start is 25 s, 106 s on a device short of RAM, and it once froze the backend), and
it stops after ``bark_idle_seconds`` unused. On the GPU, voice mode owns its lifetime.

Whisper (``AudioTranscriber``) is the fallback: every method here either works or says it
didn't, and the caller falls through.
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional

import httpx
import numpy as np

from app.config import settings
from app.logs import POLL_REQUEST, SIDECAR_RING, stamp
from app.voice.laya_sidecar import _gpu_used_mb

logger = logging.getLogger("jarvis.voice.bark")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_CUDA_CONTEXT_MB = 200.0  # on top of torch's reserved memory, as for Laya


def _resolve(path_str: str) -> Path:
    p = Path(path_str)
    return p if p.is_absolute() else (REPO_ROOT / p)


def decode_audio(audio_data: bytes) -> np.ndarray:
    """Any container PyAV reads (webm/opus, mp4/aac, wav) to 16 kHz mono float32."""
    import io

    from faster_whisper.audio import decode_audio as _decode

    return _decode(io.BytesIO(audio_data), sampling_rate=16000)


class BarkSidecar:
    def __init__(self, port: Optional[int] = None, startup_timeout: float = 120.0, request_timeout: float = 60.0):
        self.python = _resolve(settings.bark_python)
        self.script = _resolve(settings.bark_server_script)
        self.model_dir = _resolve(settings.bark_model_dir)
        self.port = port or settings.bark_port
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.startup_timeout = startup_timeout
        self.vram_mb = 0.0
        self.device: Optional[str] = None
        self._process: Optional[subprocess.Popen] = None
        self._lock = threading.RLock()
        self._client = httpx.Client(timeout=request_timeout)
        self._recent: list[str] = []
        self._failed_at = 0.0  # a start that failed isn't retried on every utterance
        self.idle_seconds = settings.bark_idle_seconds
        self._last_used = 0.0
        self._starting = False
        self._start_guard = threading.Lock()
        self._watcher: Optional[threading.Thread] = None

    @property
    def available(self) -> bool:
        return self.python.is_file() and self.script.is_file() and (self.model_dir / "model.safetensors").is_file()

    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def is_ready(self) -> bool:
        """Running and answered its health check (``device`` is set only then)."""
        return self.is_running() and self.device is not None

    def status(self) -> dict[str, Any]:
        return {"available": self.available, "running": self.is_running(), "starting": self._starting,
                "device": self.device, "idle_seconds": self.idle_seconds,
                "port": self.port, "model": str(self.model_dir), "vram_mb": round(self.vram_mb)}

    def _drain(self, stream) -> None:
        try:
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line and not POLL_REQUEST.search(line):
                    self._recent = (self._recent + [stamp(line)])[-SIDECAR_RING:]
                    logger.debug("[bark] %s", line)
        except Exception:
            pass

    def _health(self) -> Optional[dict]:
        try:
            r = self._client.get(f"{self.base_url}/health", timeout=1.0)
            return r.json() if r.status_code == 200 else None
        except Exception:
            return None

    def ensure_running(self, device: Optional[str] = None) -> bool:
        """Start the server (blocking, ~8-30 s cold). ``device`` None = whatever is running, else
        CPU; naming one that differs from the running device restarts it there. Serialized."""
        with self._lock:
            if self.is_running() and (device is None or device == self.device):
                return True
            if not self.available:
                logger.warning("Bark sidecar unavailable: python=%s script=%s model=%s",
                               self.python, self.script, self.model_dir)
                return False
            device = device or "cpu"
            if time.time() - self._failed_at < 60:
                return False
            if device == "cuda":
                from app.governor.game_mode import blocked

                if blocked():
                    return False
            if self.is_running():
                self._stop_process()
            from app.agent.process_guard import tie_to_backend
            from app.agent.runtime_process_manager import PROTECTED_PIDS

            self._reap_stale()
            cmd = [str(self.python), str(self.script), "--model", str(self.model_dir),
                   "--port", str(self.port), "--device", device]
            env = {**os.environ, "USE_TF": "0", "HF_HUB_OFFLINE": "1",
                   "HF_HOME": str(REPO_ROOT / "tools" / "hf-cache")}
            logger.info("Starting Bark sidecar: %s", " ".join(cmd))
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0,
                                        env=env, cwd=str(REPO_ROOT),
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except Exception as e:
                logger.warning("Could not start the Bark sidecar: %s", e)
                self._failed_at = time.time()
                return False
            self._process = proc
            PROTECTED_PIDS.add(proc.pid)
            tie_to_backend(proc.pid)
            threading.Thread(target=self._drain, args=(proc.stdout,), daemon=True).start()

            started = time.time()
            while time.time() - started < self.startup_timeout:
                if proc.poll() is not None:
                    logger.warning("Bark sidecar exited during startup (code %s):\n%s",
                                   proc.returncode, "\n".join(self._recent[-8:]))
                    PROTECTED_PIDS.discard(proc.pid)
                    self._process = None
                    self._failed_at = time.time()
                    return False
                health = self._health()
                if health:
                    self.device = device
                    reserved = health.get("vram_mib")
                    self.vram_mb = float(reserved) + _CUDA_CONTEXT_MB if reserved else 0.0
                    logger.info("Bark sidecar ready in %.1fs on %s (%.0f MiB VRAM)",
                                time.time() - started, device, self.vram_mb)
                    self._last_used = time.time()
                    self._watch_idle()
                    return True
                time.sleep(0.25)
            logger.warning("Bark sidecar not healthy after %.0fs; stopping it.", self.startup_timeout)
            self._stop_process()
            self._failed_at = time.time()
            return False

    def start_in_background(self, device: Optional[str] = None) -> None:
        """``ensure_running`` on its own thread; a no-op while a start is already under way."""
        with self._start_guard:
            if self._starting:
                return
            self._starting = True

        def run() -> None:
            try:
                self.ensure_running(device)
            finally:
                self._starting = False

        threading.Thread(target=run, name="bark-start", daemon=True).start()

    def _watch_idle(self) -> None:
        if self.idle_seconds <= 0 or (self._watcher is not None and self._watcher.is_alive()):
            return
        self._watcher = threading.Thread(target=self._idle_loop, name="bark-idle", daemon=True)
        self._watcher.start()

    def _idle_loop(self) -> None:
        while self.is_running():
            time.sleep(min(30.0, max(0.05, self.idle_seconds / 4)))
            self.stop_if_idle()

    def stop_if_idle(self) -> bool:
        """Stop a CPU server unused for ``idle_seconds``. On the GPU voice mode decides."""
        if self.idle_seconds <= 0 or self.device != "cpu" or time.time() - self._last_used < self.idle_seconds:
            return False
        if not self._lock.acquire(blocking=False):  # a start or stop is under way
            return False
        try:
            if self.device != "cpu" or time.time() - self._last_used < self.idle_seconds:
                return False
            logger.info("Bark sidecar idle for %.0fs; stopping it.", self.idle_seconds)
            self._stop_process()
            return True
        finally:
            self._lock.release()

    def _reap_stale(self) -> None:
        """A Bark server left by a backend that died: on Windows a new one would bind behind it."""
        import psutil

        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                cmdline = " ".join(proc.info.get("cmdline") or [])
                if "bark_server.py" in cmdline and f"--port {self.port}" in cmdline and proc.pid != os.getpid():
                    logger.info("Reaping stale Bark sidecar (pid %s)", proc.pid)
                    proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue

    def _stop_process(self) -> None:
        proc, self._process = self._process, None
        self.vram_mb = 0.0
        self.device = None
        if proc is None:
            return
        from app.agent.runtime_process_manager import PROTECTED_PIDS

        PROTECTED_PIDS.discard(proc.pid)
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        logger.info("Bark sidecar stopped.")

    def stop(self) -> None:
        with self._lock:
            self._stop_process()

    def transcribe(self, samples: np.ndarray) -> str:
        """Text for 16 kHz mono float32 ``samples``. Raises on any failure (not ready yet, server
        error): the caller falls back to Whisper. Not ready = start it in the background, never wait."""
        self._last_used = time.time()
        if not self.is_ready():
            if not self.available:
                raise RuntimeError("Bark sidecar unavailable")
            self.start_in_background()
            raise RuntimeError("Bark sidecar is starting; Whisper answers meanwhile")
        r = self._client.post(f"{self.base_url}/transcribe",
                              content=np.ascontiguousarray(samples, dtype="<f4").tobytes())
        r.raise_for_status()
        return str(r.json().get("text", "")).strip()


_instance: Optional[BarkSidecar] = None


def get_bark_sidecar() -> BarkSidecar:
    global _instance
    if _instance is None:
        _instance = BarkSidecar()
    return _instance
