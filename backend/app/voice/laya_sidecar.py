"""
Laya sidecar: the voice-mode command brain's decision model (PLAN 4.9), a GPU process.

Laya (convaiinnovations/laya, fine-tuned on Jarvis's commands into ``tools/laya-voice``) needs
torch, which lives in its own venv (``tools/laya-venv``) so the backend's stays small. This
starts ``scripts/laya_server.py`` there and answers ``predict(state, questions)`` over loopback
HTTP for ``command_brain.evaluate``.

Unlike the CPU sidecars it is not idle-stopped: voice mode starts it and stops it. Its VRAM
(~1.1 GB in fp16: weights + CUDA context) is its own reported torch reservation plus the
context (``_CUDA_CONTEXT_MB``), reported through ``vram_mb``, which the governor adds to the
chat model's share -- otherwise it reads as an external workload growing past the launch baseline and evicts MiniCPM.
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

from app.config import settings

logger = logging.getLogger("jarvis.voice.laya")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
# CUDA context + cuBLAS workspace on top of torch's reserved memory: the whole process measured
# 1114 MiB alone on the card (2026-10-04) against /health's 916 MiB reserved (2026-10-14).
_CUDA_CONTEXT_MB = 200.0


def _resolve(path_str: str) -> Path:
    p = Path(path_str)
    return p if p.is_absolute() else (REPO_ROOT / p)


def _gpu_used_mb() -> Optional[float]:
    try:
        try:
            import nvidia_ml_py as pynvml  # type: ignore
        except ImportError:
            import pynvml  # type: ignore
        pynvml.nvmlInit()
        info = pynvml.nvmlDeviceGetMemoryInfo(pynvml.nvmlDeviceGetHandleByIndex(0))
        return info.used / 2**20
    except Exception:
        return None


class LayaSidecar:
    def __init__(self, port: Optional[int] = None, startup_timeout: float = 90.0, request_timeout: float = 10.0):
        self.python = _resolve(settings.laya_python)
        self.script = _resolve(settings.laya_server_script)
        self.model_dir = _resolve(settings.laya_model_dir)
        self.port = port or settings.laya_port
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.startup_timeout = startup_timeout
        self.request_timeout = request_timeout
        self.vram_mb = 0.0
        self._process: Optional[subprocess.Popen] = None
        self._lock = threading.RLock()
        self._client = httpx.Client(timeout=request_timeout)
        self._recent: list[str] = []

    @property
    def available(self) -> bool:
        return self.python.is_file() and self.script.is_file() and (self.model_dir / "model.safetensors").is_file()

    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def status(self) -> dict[str, Any]:
        return {"available": self.available, "running": self.is_running(), "port": self.port,
                "model": str(self.model_dir), "vram_mb": round(self.vram_mb)}

    def _drain(self, stream) -> None:
        try:
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line:
                    self._recent = (self._recent + [line])[-30:]
                    logger.debug("[laya] %s", line)
        except Exception:
            pass

    def _healthy(self) -> bool:
        try:
            return self._client.get(f"{self.base_url}/health", timeout=1.0).status_code == 200
        except Exception:
            return False

    def _reported_vram_mb(self) -> Optional[float]:
        """Laya's own count (torch's reserved memory) plus its CUDA context. Unlike a card-wide
        before/after, it holds while MiniCPM loads at the same time (the parallel start read
        2694 MiB for ~1.1 GB, 2026-10-14). WDDM hides per-process VRAM from NVML."""
        try:
            reserved = self._client.get(f"{self.base_url}/health", timeout=1.0).json().get("vram_mib")
        except Exception:
            return None
        return float(reserved) + _CUDA_CONTEXT_MB if reserved else None

    def ensure_running(self) -> bool:
        """Start the server (blocking, ~3-45 s depending on the disk cache). Serialized."""
        with self._lock:
            if self.is_running():
                return True
            if not self.available:
                logger.warning("Laya sidecar unavailable: python=%s script=%s model=%s",
                               self.python, self.script, self.model_dir)
                return False
            from app.governor.game_mode import blocked

            if blocked():
                return False
            from app.agent.process_guard import tie_to_backend
            from app.agent.runtime_process_manager import PROTECTED_PIDS

            self._reap_stale()
            before = _gpu_used_mb()
            cmd = [str(self.python), str(self.script), "--model", str(self.model_dir),
                   "--port", str(self.port), "--fp16"]
            env = {**os.environ, "USE_TF": "0", "HF_HUB_OFFLINE": "1",
                   "HF_HOME": str(REPO_ROOT / "tools" / "hf-cache")}
            logger.info("Starting Laya sidecar: %s", " ".join(cmd))
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0,
                                        env=env, cwd=str(REPO_ROOT),
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except Exception as e:
                logger.warning("Could not start the Laya sidecar: %s", e)
                return False
            self._process = proc
            PROTECTED_PIDS.add(proc.pid)
            tie_to_backend(proc.pid)
            threading.Thread(target=self._drain, args=(proc.stdout,), daemon=True).start()

            started = time.time()
            while time.time() - started < self.startup_timeout:
                if proc.poll() is not None:
                    logger.warning("Laya sidecar exited during startup (code %s):\n%s",
                                   proc.returncode, "\n".join(self._recent[-8:]))
                    PROTECTED_PIDS.discard(proc.pid)
                    self._process = None
                    return False
                if self._healthy():
                    after = _gpu_used_mb()
                    self.vram_mb = self._reported_vram_mb() or (
                        max(0.0, after - before) if (before is not None and after is not None) else 1000.0)
                    logger.info("Laya sidecar ready in %.1fs (%.0f MiB VRAM)", time.time() - started, self.vram_mb)
                    return True
                time.sleep(0.25)
            logger.warning("Laya sidecar not healthy after %.0fs; stopping it.", self.startup_timeout)
            self._stop_process()
            return False

    def _reap_stale(self) -> None:
        """A Laya server left by a backend that died: on Windows a new one would bind behind it."""
        import psutil

        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                cmdline = " ".join(proc.info.get("cmdline") or [])
                if "laya_server.py" in cmdline and f"--port {self.port}" in cmdline and proc.pid != os.getpid():
                    logger.info("Reaping stale Laya sidecar (pid %s)", proc.pid)
                    proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue

    def _stop_process(self) -> None:
        proc, self._process = self._process, None
        self.vram_mb = 0.0
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
        logger.info("Laya sidecar stopped.")

    def stop(self) -> None:
        with self._lock:
            self._stop_process()

    def predict(self, state: dict, questions: dict) -> dict:
        """Laya's ``answers`` for ``questions`` about ``state``. Raises on any failure: the caller
        falls back to MiniCPM rather than act on a guess."""
        r = self._client.post(f"{self.base_url}/predict",
                              json={"state": state, "questions": questions, "max_len": 512, "head_max_len": 320})
        r.raise_for_status()
        return r.json()["answers"]


_instance: Optional[LayaSidecar] = None


def get_laya_sidecar() -> LayaSidecar:
    global _instance
    if _instance is None:
        _instance = LayaSidecar()
    return _instance
