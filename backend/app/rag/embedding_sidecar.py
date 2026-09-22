"""
Embedding sidecar: a CPU-only llama-server that turns text into vectors.

RAG's "semantic" search had nothing semantic behind it. ``sentence_transformers`` was never
installed, so ``EmbeddingService`` always fell through to its hashed bag-of-words engine: a
vector per chunk, but one where "delete probe.txt" and a function that deletes attachments
share nothing but the letters, and every query lands somewhere in the same 0.0-0.1 band of
cosine similarity. With no real relevance signal there was nothing to put a floor under, so
every WORKSPACE turn carried whatever ranked first.

This runs a small embedding GGUF (nomic-embed-text-v1.5 by default, 137M parameters, 768
dims) through the same ``llama-server.exe`` as the chat model, on its own port and on the CPU
only, the way the image captioner does. A query embeds in ~35 ms; a batch of eight 1k-token
chunks in ~0.4 s, so indexing a workspace is seconds, not minutes. Cosine similarities come
out with real separation -- against this repo's code a question's answer scores 0.71-0.97
while everything unrelated sits flat at 0.52-0.65 -- which is what makes the reranker's
floor, gap and peak cuts mean something.

Cold like the captioner: spawned on the first embedding, stopped after ``idle_seconds`` without
one. Synchronous, because the indexer and retriever are synchronous.
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

logger = logging.getLogger("jarvis.rag.embedding_sidecar")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent

# Longer than any chunk the chunker produces; keeps a stray oversized markdown section from
# tripping the server's context limit (ctx 2048 tokens ~ 8k characters of prose).
MAX_TEXT_CHARS = 6000


def _resolve(path_str: str) -> Path:
    p = Path(path_str)
    return p if p.is_absolute() else (REPO_ROOT / p)


class EmbeddingSidecar:
    """Owns the sidecar process and the "texts -> vectors" call against it."""

    def __init__(
        self,
        model_path: Optional[str] = None,
        server_exe: Optional[str] = None,
        port: Optional[int] = None,
        idle_seconds: Optional[float] = None,
        threads: Optional[int] = None,
        startup_timeout: float = 45.0,
        request_timeout: float = 120.0,
        batch_size: int = 16,
    ):
        self.model_path = _resolve(model_path or settings.rag_embedding_model_path)
        self.server_exe = _resolve(server_exe or settings.llama_server_exe)
        self.port = port or settings.rag_embedding_port
        self.idle_seconds = settings.rag_embedding_idle_seconds if idle_seconds is None else idle_seconds
        self.threads = settings.rag_embedding_threads if threads is None else threads
        self.startup_timeout = startup_timeout
        self.request_timeout = request_timeout
        self.batch_size = batch_size
        self.base_url = f"http://127.0.0.1:{self.port}"

        self._process: Optional[subprocess.Popen] = None
        self._lock = threading.RLock()
        self._last_used = 0.0
        self._idle_thread: Optional[threading.Thread] = None
        self._recent_output: list[str] = []
        self._output_lock = threading.Lock()
        self._unavailable_logged = False

    # ------------------------------------------------------------- state

    @property
    def available(self) -> bool:
        """Whether there is anything to run: enabled, and the model and server exist."""
        return bool(settings.rag_embedding_sidecar_enabled and self.model_path.is_file() and self.server_exe.is_file())

    @property
    def identity(self) -> str:
        """Names the vector space: what this sidecar's vectors are comparable with. Part of the
        index generation marker, so a model swap re-embeds instead of mixing spaces."""
        return f"llama-server:{self.model_path.name}"

    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def pid(self) -> Optional[int]:
        return self._process.pid if self.is_running() else None

    def status(self) -> dict[str, Any]:
        return {
            "enabled": bool(settings.rag_embedding_sidecar_enabled),
            "available": self.available,
            "running": self.is_running(),
            "model": str(self.model_path.relative_to(REPO_ROOT)) if self.model_path.is_relative_to(REPO_ROOT) else str(self.model_path),
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
                logger.debug("[embedder %s] %s", label, line)
        except Exception:
            pass

    def _build_command(self) -> list[str]:
        cmd = [
            str(self.server_exe),
            "--model", str(self.model_path),
            "--alias", "embedder",
            "--host", "127.0.0.1",
            "--port", str(self.port),
            "--embedding",
            "--pooling", settings.rag_embedding_pooling,
            "--ctx-size", "2048",
            # One ubatch must hold a whole input for embeddings; match it to the context.
            "--batch-size", "2048",
            "--ubatch-size", "2048",
            "--n-gpu-layers", "0",
            "--parallel", "1",
        ]
        if self.threads and self.threads > 0:
            cmd += ["--threads", str(self.threads)]
        return cmd

    def _healthy(self, timeout: float = 1.5) -> bool:
        try:
            r = httpx.get(f"{self.base_url}/health", timeout=timeout)
            return r.status_code == 200
        except Exception:
            return False

    def ensure_running(self) -> bool:
        """Start the sidecar if it is not up. Serialized, so concurrent callers share one start."""
        with self._lock:
            if self.is_running():
                return True
            if not self.available:
                if not self._unavailable_logged:
                    logger.info(
                        "Embedding sidecar unavailable (enabled=%s, model=%s): RAG falls back to hashed vectors.",
                        settings.rag_embedding_sidecar_enabled, self.model_path,
                    )
                    self._unavailable_logged = True
                return False

            from app.agent.process_guard import tie_to_backend
            from app.agent.runtime_process_manager import PROTECTED_PIDS

            self._reap_stale_sidecars()
            cmd = self._build_command()
            logger.info("Starting embedding sidecar: %s", " ".join(cmd))
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
                logger.warning("Could not start embedding sidecar (%s): %s", self.server_exe, e)
                return False
            self._process = proc
            PROTECTED_PIDS.add(proc.pid)
            tie_to_backend(proc.pid)
            for stream, label in ((proc.stdout, "stdout"), (proc.stderr, "stderr")):
                threading.Thread(target=self._drain, args=(stream, label), daemon=True).start()

            started = time.time()
            while time.time() - started < self.startup_timeout:
                if proc.poll() is not None:
                    time.sleep(0.2)
                    with self._output_lock:
                        tail = "\n".join(self._recent_output[-8:])
                    logger.warning("Embedding sidecar exited during startup (code %s).\n%s", proc.returncode, tail)
                    PROTECTED_PIDS.discard(proc.pid)
                    self._process = None
                    return False
                if self._healthy():
                    logger.info("Embedding sidecar ready at %s (%s)", self.base_url, self.model_path.name)
                    self._touch()
                    return True
                time.sleep(0.25)

            logger.warning("Embedding sidecar did not become healthy within %.0fs; stopping it.", self.startup_timeout)
            self._stop_process()
            return False

    def _reap_stale_sidecars(self) -> None:
        """Kill embedding servers left behind by a backend that did not shut down cleanly (our
        model file on their command line); on Windows a new server binds the same port behind
        them and they would keep answering forever."""
        import psutil

        marker = str(self.model_path).lower()
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                if "llama-server" not in (proc.info.get("name") or "").lower():
                    continue
                cmdline = " ".join(proc.info.get("cmdline") or []).lower()
                if marker in cmdline and proc.pid != os.getpid():
                    logger.info("Reaping stale embedding sidecar (pid %s)", proc.pid)
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except psutil.TimeoutExpired:
                        proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            except Exception as e:
                logger.debug("Stale embedder check failed: %s", e)

    def _stop_process(self) -> None:
        proc = self._process
        self._process = None
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
        logger.info("Embedding sidecar stopped.")

    def stop(self) -> None:
        with self._lock:
            self._stop_process()

    def _touch(self) -> None:
        self._last_used = time.time()
        if self.idle_seconds > 0 and (self._idle_thread is None or not self._idle_thread.is_alive()):
            self._idle_thread = threading.Thread(target=self._idle_watch, daemon=True, name="embedder-idle")
            self._idle_thread.start()

    def _idle_watch(self) -> None:
        """Shut the sidecar down once nobody has asked for a vector in a while."""
        while self.is_running():
            remaining = self.idle_seconds - (time.time() - self._last_used)
            if remaining <= 0:
                logger.info("Embedding sidecar idle for %.0fs; shutting it down.", self.idle_seconds)
                with self._lock:
                    self._stop_process()
                return
            time.sleep(min(remaining, 30.0))

    # ---------------------------------------------------------- embedding

    def embed(self, texts: list[str]) -> Optional[list[list[float]]]:
        """Vectors for ``texts`` in order (already prefixed by the caller), or None when the
        sidecar cannot serve them -- the caller decides what to fall back to."""
        if not texts:
            return []
        if not self.ensure_running():
            return None
        out: list[list[float]] = []
        try:
            for start in range(0, len(texts), self.batch_size):
                batch = [t[:MAX_TEXT_CHARS] if t else " " for t in texts[start:start + self.batch_size]]
                r = httpx.post(
                    f"{self.base_url}/v1/embeddings", json={"input": batch}, timeout=self.request_timeout
                )
                r.raise_for_status()
                data = sorted(r.json()["data"], key=lambda d: d.get("index", 0))
                out.extend([list(map(float, d["embedding"])) for d in data])
            self._touch()
        except Exception as e:
            logger.warning("Embedding request failed (%s); falling back to hashed vectors for this call.", e)
            return None
        if len(out) != len(texts):
            logger.warning("Embedding sidecar returned %d vectors for %d texts.", len(out), len(texts))
            return None
        return out


_SIDECAR: Optional[EmbeddingSidecar] = None
_SIDECAR_LOCK = threading.Lock()


def get_embedding_sidecar() -> EmbeddingSidecar:
    global _SIDECAR
    with _SIDECAR_LOCK:
        if _SIDECAR is None:
            _SIDECAR = EmbeddingSidecar()
        return _SIDECAR
