import asyncio
import logging
import os
import sys
import threading
import subprocess
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional
import httpx
from app.config import settings

logger = logging.getLogger("jarvis.agent.process_manager")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent


def _reasoning_flag_from_kwargs(kwargs_json: Optional[str]) -> Optional[str]:
    """Extracts an `enable_thinking` bool out of a chat-template-kwargs JSON string and maps it to
    llama-server's `--reasoning on|off` flag. Returns None (leave --reasoning unset, i.e. 'auto')
    when there's nothing to parse or no such key -- never raises on malformed JSON."""
    if not kwargs_json:
        return None
    try:
        import json
        parsed = json.loads(kwargs_json)
    except Exception:
        return None
    if not isinstance(parsed, dict) or "enable_thinking" not in parsed:
        return None
    return "on" if parsed["enable_thinking"] else "off"


# llama-server processes that are ours but not the chat model -- the image captioner sidecar --
# so an emergency sweep of "every llama-server on the machine" leaves them alone.
PROTECTED_PIDS: set[int] = set()


def resolve_repo_path(path_str: str) -> Path:
    """Resolve a path relative to the repo root if it's relative, otherwise return as absolute."""
    p = Path(path_str)
    if not p.is_absolute():
        p = (REPO_ROOT / p).resolve()
    return p


class RuntimeProcessManager:
    """
    Deterministic OS process management for llama-server.exe.
    Controls process startup, model switching, health verification,
    and 100% process-based GPU VRAM eviction.
    """

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        server_exe: Optional[str] = None,
        main_model_path: Optional[str] = None,
        fast_model_path: Optional[str] = None,
        ctx_size: Optional[int] = None,
        gpu_layers: Optional[int] = None,
        use_mmap: Optional[bool] = None,
        startup_timeout: Optional[float] = None,
        extra_args: Optional[list[str]] = None,
    ):
        self.host = host or settings.llama_host
        self.port = port if port is not None else settings.llama_port
        self.server_exe = server_exe or settings.llama_server_exe
        self.main_model_path = main_model_path or settings.llama_main_model_path
        self.fast_model_path = fast_model_path or settings.llama_fast_model_path
        self.ctx_size = ctx_size
        self.gpu_layers = gpu_layers if gpu_layers is not None else settings.llama_n_gpu_layers
        self.use_mmap = use_mmap if use_mmap is not None else settings.llama_use_mmap
        self.startup_timeout = startup_timeout if startup_timeout is not None else settings.llama_startup_timeout_seconds
        self.extra_args = list(extra_args if extra_args is not None else settings.llama_extra_args)
        self._loaded_projector: Optional[Path] = None


        self._process: Optional[subprocess.Popen] = None
        self._current_model_kind: Optional[str] = None
        self._externally_managed: bool = False
        self._lock = asyncio.Lock()
        # Bounded so a long-running server cannot grow it without limit; only the tail matters.
        self._recent_output: deque[str] = deque(maxlen=200)
        self._output_lock = threading.Lock()

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def current_model_kind(self) -> Optional[str]:
        return self._current_model_kind

    @property
    def is_externally_managed(self) -> bool:
        return self._externally_managed

    def resolve_model_path(self, model_kind: str) -> tuple[Path, str]:
        """
        Resolve model file path and alias based on kind ('main', 'fast', or direct path/filename).
        Returns (resolved_path, alias).

        A slot chosen through the model catalogue wins over the configured .env path, so a
        selection made in the UI survives a backend restart. An unset slot falls through to
        settings exactly as before.
        """
        from app.agent.model_catalog import get_model_catalog

        catalog = get_model_catalog()
        kind_norm = (model_kind or "main").strip().lower()
        if kind_norm in ("main", "9b", "qwen3.5-9b", "default"):
            target_path_str = catalog.selected("main") or self.main_model_path
            alias = "main"
        elif kind_norm in ("fast", "4b", "qwen3.5-4b"):
            target_path_str = catalog.selected("fast") or self.fast_model_path
            alias = "fast"
        else:
            target_path_str = model_kind
            alias = Path(model_kind).stem

        resolved = resolve_repo_path(target_path_str)
        if not resolved.exists() and alias in ("main", "fast"):
            # The configured file moved or was deleted (models now live one-per-folder, so a path
            # like models/Qwen3.5-4B-*.gguf can go stale). Fall back to the first catalogued model
            # of the slot's usual size -- discover() lists recommended ones first, so a model in
            # its own folder beats a copy buried in a vendor mirror.
            wanted_family = "4B" if alias == "fast" else "9B"
            for cand in catalog.discover():
                if cand.family == wanted_family:
                    logger.warning(
                        "Configured %s model %s does not exist; using %s instead.",
                        alias, resolved, cand.id,
                    )
                    return REPO_ROOT / cand.id, alias
        elif not resolved.exists() and alias not in ("main", "fast"):
            # An arbitrary model_kind is treated as a path, which is right for a real file and
            # badly wrong for anything else: a stale model *identifier* from another runtime
            # (e.g. "prism-ml/bonsai-27b") became D:/JARVIS/prism-ml/bonsai-27b and llama-server
            # died trying to open it. Nothing downstream can do anything useful with a path that
            # cannot exist, so fall back to the main slot and say so.
            logger.warning(
                "Model kind '%s' does not resolve to an existing file (%s); falling back to the "
                "main model. This usually means a caller passed a model identifier from a "
                "different runtime rather than a path or a slot name.",
                model_kind, resolved,
            )
            return self.resolve_model_path("main")
        return resolved, alias

    def resolve_projector_path(self, model_path: Path) -> Optional[Path]:
        """
        The ``--mmproj`` file to load with a model, or None for a text-only launch.

        Pairing is by directory (see ``ModelCatalog.projector_for``); ``LLAMA_MMPROJ_ENABLED=false``
        turns it off globally for anyone who would rather keep the projector's VRAM.
        """
        if not settings.llama_mmproj_enabled:
            return None
        from app.agent.model_catalog import get_model_catalog

        projector = get_model_catalog().projector_for(Path(model_path))
        return projector.resolve() if projector else None

    @property
    def loaded_projector(self) -> Optional[Path]:
        """The ``--mmproj`` file the Jarvis-spawned server is currently running with, if any."""
        return self._loaded_projector

    def has_vision(self, model_kind: str = "main") -> bool:
        """
        Whether a request for ``model_kind`` would be served by a model with a projector.

        If that slot is the one already loaded, the answer is what actually loaded -- a launch
        that had to drop its projector (see ``ensure_running``) reports False even though a
        projector sits on disk. Otherwise it is what the next launch would attempt.
        """
        try:
            resolved_model, alias = self.resolve_model_path(model_kind)
        except Exception:
            return False
        if self.is_running() and self._current_model_kind in (alias, model_kind):
            return self._loaded_projector is not None
        return self.resolve_projector_path(resolved_model) is not None

    async def health_check(self, timeout: float = 3.0) -> bool:
        """Probe GET /v1/models endpoint."""
        url = f"{self.base_url}/v1/models"
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(url)
                return resp.status_code == 200
        except Exception:
            return False

    def is_running(self) -> bool:
        """Check if child process is active and has not terminated."""
        if self._process is not None:
            return self._process.poll() is None
        return self._externally_managed

    def _drain_sync_stream(self, stream, stream_name: str) -> None:
        """
        Drain a child output stream in a background thread to prevent pipe buffer stalls, keeping
        the most recent lines so a startup failure can report what llama-server actually said.

        Without the buffer a crash surfaced as nothing but 'exited prematurely with code N' --
        the child's own diagnosis went to a debug logger that is off in normal runs, which made
        every startup failure look like an unexplainable environment quirk.
        """
        if not stream:
            return
        try:
            for line in iter(stream.readline, b""):
                decoded = line.decode("utf-8", errors="replace").rstrip()
                if decoded:
                    logger.debug("[llama-server %s] %s", stream_name, decoded)
                    with self._output_lock:
                        self._recent_output.append(f"[{stream_name}] {decoded}")
        except Exception:
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def _tail_output(self, limit: int = 12) -> str:
        """The last few lines the child emitted, for inclusion in a startup error."""
        with self._output_lock:
            lines = list(self._recent_output)[-limit:]
        return "\n".join(lines)

    async def ensure_running(self, model_kind: str = "main") -> bool:
        """
        Ensure llama-server is online with the requested model_kind.
        If unhealthy or missing, spawn the process and wait for ready state.
        """
        resolved_exe = resolve_repo_path(self.server_exe)
        resolved_model, alias = self.resolve_model_path(model_kind)

        async with self._lock:
            # 1. If health check passes
            if await self.health_check(timeout=2.0):
                if self._current_model_kind in (alias, model_kind) or self._current_model_kind is None:
                    self._current_model_kind = model_kind
                    # Invariant: _externally_managed must be True ONLY if the server was discovered
                    # already running externally. If Jarvis spawned self._process, it remains False.
                    if self._process is None or self._process.poll() is not None:
                        self._externally_managed = True
                    else:
                        self._externally_managed = False
                    return True


                # Different model kind requested -> trigger switch. Check the target is loadable
                # BEFORE stopping what is already serving: otherwise one bad request replaces a
                # working model with nothing, which is exactly how a stale model identifier took
                # down a healthy server mid-conversation.
                if not resolved_model.exists():
                    raise RuntimeError(
                        f"Refusing to switch to '{model_kind}': no model file at {resolved_model}. "
                        f"Keeping the currently loaded '{self._current_model_kind}'."
                    )

                logger.info(
                    "Switching running llama-server model from '%s' to '%s' (alias: %s)",
                    self._current_model_kind,
                    model_kind,
                    alias
                )
                await self._stop_internal()


            # 2. Server not running or needs restart with new model
            ctx_size_for_model = (
                self.ctx_size
                if self.ctx_size is not None
                else (
                    settings.llama_ctx_size_fast if alias == "fast" else settings.llama_ctx_size_main
                )
            )

            cmd = [
                str(resolved_exe),
                "--model", str(resolved_model),
                "--alias", alias,
                "--host", str(self.host),
                "--port", str(self.port),
                "--ctx-size", str(ctx_size_for_model),
                "--n-gpu-layers", str(self.gpu_layers),
                "--parallel", "1",
            ]
            if not self.use_mmap:
                cmd.append("--no-mmap")
            projector = self.resolve_projector_path(resolved_model)
            projector_args: list[str] = []
            if projector is not None:
                projector_args = ["--mmproj", str(projector)]
                if not settings.llama_mmproj_offload:
                    projector_args.append("--no-mmproj-offload")
            cmd.extend(projector_args)
            if self.extra_args:
                cmd.extend(self.extra_args)

            # NOTE: this build's llama-server reads chat-template-kwargs from the environment
            # variable LLAMA_ARG_CHAT_TEMPLATE_KWARGS, not LLAMA_CHAT_TEMPLATE_KWARGS (confirmed via
            # `llama-server.exe --help`) -- the latter name is silently ignored, which is why the
            # enable_thinking toggle never actually took effect via .env alone. Passed as a real CLI
            # arg here instead of relying on getting an env var name exactly right a second time.
            kwargs_for_alias = (
                settings.llama_chat_template_kwargs_fast
                if alias == "fast"
                else settings.llama_chat_template_kwargs
            )
            if kwargs_for_alias:
                cmd.extend(["--chat-template-kwargs", kwargs_for_alias])

            # Belt-and-suspenders: --reasoning is llama.cpp's own first-class on/off/auto switch,
            # independent of whether a given jinja template actually honours enable_thinking inside
            # --chat-template-kwargs. Derived from the same enable_thinking value so .env stays the
            # single place this is configured.
            reasoning_flag = _reasoning_flag_from_kwargs(kwargs_for_alias)
            if reasoning_flag is not None:
                cmd.extend(["--reasoning", reasoning_flag])

            # The projector is the one part of the launch that is optional: it needs its own
            # ~0.7-0.9 GB of VRAM (or RAM with --no-mmproj-offload) on top of the model, and on an
            # 8 GB card with other apps holding memory it can fail to allocate. When that happens
            # the model is relaunched text-only rather than leaving the user with no model at all;
            # ``has_vision`` reports the projector as absent so images fall back to file notes.
            try:
                return await self._spawn_and_wait(cmd, resolved_exe, model_kind, alias, projector)
            except RuntimeError as exc:
                if not projector_args:
                    raise
                logger.warning(
                    "llama-server failed to start with projector %s; retrying text-only. Cause: %s",
                    projector.name if projector else projector_args, exc,
                )
                text_only_cmd = [arg for arg in cmd if arg not in projector_args]
                return await self._spawn_and_wait(text_only_cmd, resolved_exe, model_kind, alias, None)

    async def _spawn_and_wait(
        self,
        cmd: list[str],
        resolved_exe: Path,
        model_kind: str,
        alias: str,
        projector: Optional[Path],
    ) -> bool:
        """Spawns llama-server with ``cmd`` and waits for it to become healthy. Raises RuntimeError
        (with the child's last output) if it exits early or never comes up."""
        child_env = os.environ.copy()

        with self._output_lock:
            self._recent_output.clear()

        logger.info("Spawning llama-server process: %s", " ".join(cmd))
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                env=child_env
            )
        except Exception as e:
            logger.error("Failed to spawn llama-server binary at '%s': %s", resolved_exe, e)
            raise RuntimeError(f"Failed to spawn llama-server ({resolved_exe}): {e}") from e

        self._process = proc
        self._current_model_kind = model_kind
        self._externally_managed = False
        # Die with the backend, so a crash cannot leave the old weights serving on the port.
        from app.agent.process_guard import tie_to_backend
        tie_to_backend(proc.pid)

        # Start background pipe drainers
        t_out = threading.Thread(target=self._drain_sync_stream, args=(proc.stdout, "stdout"), daemon=True)
        t_err = threading.Thread(target=self._drain_sync_stream, args=(proc.stderr, "stderr"), daemon=True)
        t_out.start()
        t_err.start()

        # Poll health check until startup timeout
        start_time = time.time()
        poll_interval = 0.5
        while time.time() - start_time < self.startup_timeout:
            if proc.poll() is not None:
                # Give the drain threads a moment to flush what the child said on its way out.
                await asyncio.sleep(0.2)
                detail = self._tail_output()
                raise RuntimeError(
                    f"llama-server exited prematurely with code {proc.returncode} during startup."
                    + (f"\n{detail}" if detail else "")
                )
            if await self.health_check(timeout=1.5):
                self._loaded_projector = projector
                logger.info(
                    "llama-server successfully started and healthy at %s (model: %s, vision: %s)",
                    self.base_url, alias, projector.name if projector else "off",
                )
                return True
            await asyncio.sleep(poll_interval)

        # Startup timed out -> kill process
        logger.error("llama-server startup timed out after %.1fs", self.startup_timeout)
        detail = self._tail_output()
        await self._stop_internal()
        raise RuntimeError(
            f"llama-server failed to become healthy within {self.startup_timeout}s."
            + (f"\n{detail}" if detail else "")
        )

    async def _stop_internal(self, sweep_all: bool = False) -> bool:
        """
        Internal worker to stop the process without acquiring the lock.
        By default (sweep_all=False), terminates ONLY the tracked child process spawned by Jarvis.
        If sweep_all=True is explicitly passed (e.g. emergency cleanup of stuck orphans),
        a warning is logged and all system llama-server instances are terminated.
        """
        self._current_model_kind = None
        self._loaded_projector = None
        self._externally_managed = False

        # 1. Terminate tracked subprocess if present
        if self._process is not None:
            proc = self._process
            self._process = None
            if proc.poll() is None:
                logger.info("Terminating tracked llama-server process (PID %s)...", proc.pid)
                try:
                    proc.terminate()
                    for _ in range(30):
                        if proc.poll() is not None:
                            break
                        await asyncio.sleep(0.1)
                    else:
                        proc.kill()
                        proc.wait()
                except Exception as e:
                    logger.debug("Error terminating tracked process: %s", e)

        # 2. Sweep all llama-server instances ONLY if explicitly requested
        if sweep_all:
            logger.warning("Emergency process sweep requested: terminating all system-wide llama-server processes.")
            import psutil
            try:
                for p in psutil.process_iter(['pid', 'name']):
                    try:
                        p_name = (p.info.get('name') or "").lower()
                        if "llama-server" in p_name and p.pid not in PROTECTED_PIDS:
                            logger.info("Terminating orphaned llama-server process (PID %s)...", p.pid)
                            p.terminate()
                            try:
                                p.wait(timeout=3)
                            except psutil.TimeoutExpired:
                                p.kill()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
            except Exception as e:
                logger.debug("Error scanning for llama-server processes: %s", e)

        logger.info("llama-server stop routine complete. GPU VRAM released.")
        return True

    async def reload(self, model_kind: str = "main") -> bool:
        """
        Restart llama-server on ``model_kind``, even when the requested kind is the one already
        running -- which ``ensure_running`` deliberately will not do, since its health check
        short-circuits on a healthy server of the same kind. Changing which *file* a slot points
        at leaves the kind unchanged, so switching models needs this explicit path.

        When the running server was started outside Jarvis, stopping only the tracked child would
        leave it serving the old weights and the caller would see a successful switch that
        changed nothing. In that case the sweep is the only way to actually free the port, so it
        is used rather than reported as success.
        """
        # ``_externally_managed`` is only learned by a health check, so a backend that has not
        # served a turn yet does not know a foreign server owns the port. Ask the port directly:
        # anything healthy there that is not our own live child is foreign. (On Windows a second
        # llama-server can bind an occupied port without error and sit unreachable behind the
        # first, so skipping this makes a switch report success while the old weights keep serving.)
        own_child_alive = self._process is not None and self._process.poll() is None
        sweep = self._externally_managed or (not own_child_alive and await self.health_check(timeout=2.0))
        if sweep:
            logger.info(
                "Reloading an externally-managed llama-server; sweeping llama-server processes "
                "so the port is actually released for the new model."
            )
        await self.stop(sweep_all=sweep)
        return await self.ensure_running(model_kind)

    async def stop(self, sweep_all: bool = False) -> bool:
        """
        Gracefully terminate or kill the llama-server process.
        This provides deterministic 100% GPU VRAM release.
        """
        async with self._lock:
            return await self._stop_internal(sweep_all=sweep_all)


    async def switch_model(self, model_kind: str) -> bool:
        """
        Stop current running model and switch to requested model_kind.
        """
        async with self._lock:
            if self.is_running() and self._current_model_kind == model_kind:
                return True
            await self._stop_internal()
            # Release lock before ensure_running to avoid deadlocks
        return await self.ensure_running(model_kind)


_process_manager_instance: Optional[RuntimeProcessManager] = None


def get_runtime_process_manager() -> RuntimeProcessManager:
    global _process_manager_instance
    if _process_manager_instance is None:
        _process_manager_instance = RuntimeProcessManager()
    return _process_manager_instance


def reset_runtime_process_manager() -> None:
    global _process_manager_instance
    _process_manager_instance = None
