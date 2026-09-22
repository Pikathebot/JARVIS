"""
Image captioner sidecar: a second, CPU-only llama-server that turns images into text.

The main model can only *see* when it has a projector loaded (``--mmproj``). Everything else
-- a text-only GGUF on the fast slot, a Heavy Mode turn on OpenRouter, a launch that had to
drop its projector for VRAM -- gets images as file paths, which is to say not at all. This
sidecar runs a small vision model (SmolVLM-500M by default) on the CPU, so it never competes
with the chat model for the 8 GB card, and produces a description that goes on the turn as
attachment text the way a PDF's contents would.

It is always cold: spawned on the first image, shut down again after ``idle_seconds`` without
one. A description takes a few seconds on the CPU, which is the trade for costing no VRAM.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional

import httpx

from app.config import settings

logger = logging.getLogger("jarvis.agent.captioner")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent

IMAGE_MIMES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
}

DEFAULT_PROMPT = (
    "Describe this image in detail for someone who cannot see it: the main subject, the layout, "
    "every piece of visible text quoted verbatim, and the colours of the background and each "
    "element. Two or three sentences."
)


def _resolve(path_str: str) -> Path:
    p = Path(path_str)
    return p if p.is_absolute() else (REPO_ROOT / p).resolve()


def _attachment_field(att: Any, name: str) -> Any:
    return getattr(att, name, None) or (att.get(name) if isinstance(att, dict) else None)


def is_image_attachment(att: Any) -> bool:
    fname = _attachment_field(att, "filename") or _attachment_field(att, "path") or ""
    return os.path.splitext(str(fname))[1].lower() in IMAGE_MIMES


class ImageCaptioner:
    """Owns the sidecar process and the "image -> description" call against it."""

    def __init__(
        self,
        model_path: Optional[str] = None,
        mmproj_path: Optional[str] = None,
        server_exe: Optional[str] = None,
        port: Optional[int] = None,
        idle_seconds: Optional[float] = None,
        threads: Optional[int] = None,
        startup_timeout: float = 60.0,
        request_timeout: float = 120.0,
        max_images_per_turn: int = 4,
    ):
        self.model_path = _resolve(model_path or settings.captioner_model_path)
        # Explicit projector, or None to look for mmproj-*.gguf beside the model on each use
        # (so one that is still downloading at startup is picked up once it lands).
        self._explicit_mmproj = _resolve(mmproj_path) if mmproj_path else None
        self.server_exe = _resolve(server_exe or settings.llama_server_exe)
        self.port = port or settings.captioner_port
        self.idle_seconds = settings.captioner_idle_seconds if idle_seconds is None else idle_seconds
        self.threads = settings.captioner_threads if threads is None else threads
        self.startup_timeout = startup_timeout
        self.request_timeout = request_timeout
        self.max_images_per_turn = max_images_per_turn
        self.base_url = f"http://127.0.0.1:{self.port}"

        self._process: Optional[subprocess.Popen] = None
        self._lock = asyncio.Lock()
        self._last_used = 0.0
        self._idle_task: Optional[asyncio.Task] = None
        self._recent_output: list[str] = []
        self._output_lock = threading.Lock()

    # ------------------------------------------------------------- state

    @staticmethod
    def _find_projector(model_path: Path) -> Optional[Path]:
        if not model_path.parent.is_dir():
            return None
        candidates = sorted(model_path.parent.glob("mmproj*.gguf"))
        return candidates[0] if candidates else None

    @property
    def mmproj_path(self) -> Optional[Path]:
        return self._explicit_mmproj or self._find_projector(self.model_path)

    @property
    def available(self) -> bool:
        """Whether there is anything to run: enabled, and both files exist."""
        return bool(
            settings.captioner_enabled
            and self.model_path.is_file()
            and self.mmproj_path is not None
            and self.mmproj_path.is_file()
        )

    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def pid(self) -> Optional[int]:
        return self._process.pid if self.is_running() else None

    def status(self) -> dict[str, Any]:
        return {
            "enabled": bool(settings.captioner_enabled),
            "available": self.available,
            "running": self.is_running(),
            "model": str(self.model_path.relative_to(REPO_ROOT)) if self.model_path.is_relative_to(REPO_ROOT) else str(self.model_path),
            "projector": (
                str(self.mmproj_path.relative_to(REPO_ROOT))
                if self.mmproj_path and self.mmproj_path.is_relative_to(REPO_ROOT)
                else (str(self.mmproj_path) if self.mmproj_path else None)
            ),
            "port": self.port,
            "idle_seconds": self.idle_seconds,
        }

    # ----------------------------------------------------------- process

    def _drain(self, stream, label: str) -> None:
        try:
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue
                with self._output_lock:
                    self._recent_output.append(line)
                    del self._recent_output[:-40]
                logger.debug("[captioner %s] %s", label, line)
        except Exception:
            pass

    def _build_command(self) -> list[str]:
        cmd = [
            str(self.server_exe),
            "--model", str(self.model_path),
            "--mmproj", str(self.mmproj_path),
            "--alias", "captioner",
            "--host", "127.0.0.1",
            "--port", str(self.port),
            "--ctx-size", "4096",
            "--n-gpu-layers", "0",
            "--no-mmproj-offload",
            "--parallel", "1",
        ]
        if self.threads and self.threads > 0:
            cmd += ["--threads", str(self.threads)]
        return cmd

    async def _healthy(self, timeout: float = 1.5) -> bool:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.get(f"{self.base_url}/health")
                return r.status_code == 200
        except Exception:
            return False

    async def ensure_running(self) -> bool:
        """Start the sidecar if it is not up. Serialized, so two images in flight share one start."""
        async with self._lock:
            if self.is_running():
                return True
            if not self.available:
                return False

            from app.agent.process_guard import tie_to_backend
            from app.agent.runtime_process_manager import PROTECTED_PIDS

            self._reap_stale_sidecars()
            cmd = self._build_command()
            logger.info("Starting image captioner: %s", " ".join(cmd))
            with self._output_lock:
                self._recent_output.clear()
            try:
                # CPU-only means no CUDA context either: with the card merely *visible*, the CUDA
                # build creates one at startup and holds ~170 MB of VRAM it never uses -- enough
                # to be the margin by which the chat model's projector no longer fits. Hiding the
                # device ("-1", not "": empty is ignored) makes ggml report no CUDA device and
                # stay on the CPU, which is where this process belongs anyway.
                env = {**os.environ, "CUDA_VISIBLE_DEVICES": "-1"}
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, env=env)
            except Exception as e:
                logger.warning("Could not start image captioner (%s): %s", self.server_exe, e)
                return False
            self._process = proc
            PROTECTED_PIDS.add(proc.pid)
            tie_to_backend(proc.pid)
            for stream, label in ((proc.stdout, "stdout"), (proc.stderr, "stderr")):
                threading.Thread(target=self._drain, args=(stream, label), daemon=True).start()

            started = time.time()
            while time.time() - started < self.startup_timeout:
                if proc.poll() is not None:
                    await asyncio.sleep(0.2)
                    with self._output_lock:
                        tail = "\n".join(self._recent_output[-8:])
                    logger.warning("Image captioner exited during startup (code %s).\n%s", proc.returncode, tail)
                    PROTECTED_PIDS.discard(proc.pid)
                    self._process = None
                    return False
                if await self._healthy():
                    logger.info("Image captioner ready at %s (%s)", self.base_url, self.model_path.name)
                    self._touch()
                    return True
                await asyncio.sleep(0.5)

            logger.warning("Image captioner did not become healthy within %.0fs; stopping it.", self.startup_timeout)
            await self._stop_process()
            return False

    def _reap_stale_sidecars(self) -> None:
        """
        Kill captioner processes left behind by a backend that did not shut down cleanly. They
        are recognisable by our model file on their command line; on Windows a new server can
        bind the same port behind them, so they would otherwise keep answering forever.
        """
        import psutil

        marker = str(self.model_path).lower()
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                if "llama-server" not in (proc.info.get("name") or "").lower():
                    continue
                cmdline = " ".join(proc.info.get("cmdline") or []).lower()
                if marker in cmdline and proc.pid != os.getpid():
                    logger.info("Reaping stale image captioner (pid %s)", proc.pid)
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except psutil.TimeoutExpired:
                        proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            except Exception as e:
                logger.debug("Stale captioner check failed: %s", e)

    async def _stop_process(self) -> None:
        proc = self._process
        self._process = None
        if proc is None:
            return
        from app.agent.runtime_process_manager import PROTECTED_PIDS

        PROTECTED_PIDS.discard(proc.pid)
        if proc.poll() is None:
            try:
                proc.terminate()
                await asyncio.to_thread(proc.wait, 5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        logger.info("Image captioner stopped.")

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_process()
        if self._idle_task:
            self._idle_task.cancel()
            self._idle_task = None

    def _touch(self) -> None:
        self._last_used = time.time()
        if self.idle_seconds > 0 and (self._idle_task is None or self._idle_task.done()):
            try:
                self._idle_task = asyncio.get_running_loop().create_task(self._idle_watch())
            except RuntimeError:
                pass

    async def _idle_watch(self) -> None:
        """Shut the sidecar down once nobody has asked for a description in a while."""
        while self.is_running():
            remaining = self.idle_seconds - (time.time() - self._last_used)
            if remaining <= 0:
                logger.info("Image captioner idle for %.0fs; shutting it down.", self.idle_seconds)
                async with self._lock:
                    await self._stop_process()
                return
            await asyncio.sleep(min(remaining, 30.0))

    # -------------------------------------------------------- describing

    async def describe(self, image_path: Path, prompt: str = DEFAULT_PROMPT) -> Optional[str]:
        """One image in, one paragraph out. None when the sidecar is unavailable or fails."""
        image_path = Path(image_path)
        mime = IMAGE_MIMES.get(image_path.suffix.lower())
        if mime is None or not image_path.is_file():
            return None
        if not await self.ensure_running():
            return None

        encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
        payload = {
            "model": "captioner",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
                ],
            }],
            "max_tokens": 300,
            "temperature": 0.2,
        }
        started = time.time()
        try:
            async with httpx.AsyncClient(timeout=self.request_timeout) as client:
                r = await client.post(f"{self.base_url}/v1/chat/completions", json=payload)
                r.raise_for_status()
                text = (r.json()["choices"][0]["message"].get("content") or "").strip()
        except Exception as e:
            logger.warning("Image captioner failed on %s: %s", image_path.name, e)
            return None
        finally:
            self._touch()
        logger.info("Captioned %s in %.1fs", image_path.name, time.time() - started)
        return text or None

    async def annotate(self, attachments: list[Any]) -> list[Any]:
        """
        Replace each image attachment with a copy carrying the description as inline ``content``,
        so the context manager includes it as attachment text. Non-images pass through untouched;
        an image the sidecar could not describe stays as it was (the model still gets its path).
        """
        if not attachments or not self.available:
            return attachments
        out: list[Any] = []
        described = 0
        for att in attachments:
            if not is_image_attachment(att) or described >= self.max_images_per_turn:
                out.append(att)
                continue
            fpath = _attachment_field(att, "path") or ""
            p = Path(str(fpath))
            if not p.is_file():
                ws = Path(settings.workspace_path).resolve() / str(fpath)
                p = ws if ws.is_file() else p
            description = await self.describe(p)
            if not description:
                out.append(att)
                continue
            described += 1
            fname = _attachment_field(att, "filename") or p.name
            # Framed as what the image shows, not as an apology: a model told "the current model
            # cannot see images" tends to say exactly that back instead of using the description.
            content = (
                f"Image contents (as seen by a vision model; treat this as what the picture shows): "
                f"{description}\n[File on disk: {fpath}]"
            )
            if isinstance(att, dict):
                out.append({**att, "content": content})
            else:
                out.append({"id": _attachment_field(att, "id"), "filename": fname, "path": fpath, "content": content})
        return out


_captioner: Optional[ImageCaptioner] = None


def get_image_captioner() -> ImageCaptioner:
    global _captioner
    if _captioner is None:
        _captioner = ImageCaptioner()
    return _captioner
