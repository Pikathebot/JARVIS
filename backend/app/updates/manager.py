"""
Tool updates from Settings: llama.cpp and the Python packages behind voice.

Nothing installs until the user presses Update. One job at a time; while one swaps files the
``tool-update`` hold (``game_mode.hold``) keeps the model and sidecars from restarting under it.
llama.cpp is verified by starting the chat model on the new build; if that fails the previous
build is put back and started instead. Restart-surviving state -- queued installs, what a roll
back would restore, the last result per tool -- is ``data/tool_updates.json``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import httpx
from packaging.version import InvalidVersion, Version

from app.agent.model_catalog import REPO_ROOT
from app.governor.game_mode import blocked, hold
from app.updates import llama_cpp, packages
from app.updates.packages import Venv

logger = logging.getLogger(__name__)

LLAMA_ID = "llama-cpp"
HOLD_KEY = "tool-update"
STATE_PATH = Path(os.environ.get("JARVIS_TOOL_UPDATES_PATH") or REPO_ROOT / "data" / "tool_updates.json")
PYPI_URL = "https://pypi.org/pypi/{name}/json"
_HEADERS = {"User-Agent": "Jarvis-tool-updates", "Accept": "application/vnd.github+json"}
ACTIVE = ("running",)


@dataclass
class UpdateJob:
    id: str
    tool: str
    action: str  # update | rollback
    status: str = "running"  # running | done | failed | cancelled
    stage: str = "Starting"
    bytes_done: int = 0
    bytes_total: int = 0
    message: Optional[str] = None
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class UpdateManager:
    def __init__(
        self,
        *,
        llama_dir: Path,
        downloads_dir: Path,
        state_path: Path = STATE_PATH,
        release_all: Callable[[], Awaitable[Any]],
        release_voice: Callable[[], Awaitable[Any]],
        relaunch: Callable[[], Awaitable[bool]],
        transport: Optional[httpx.AsyncBaseTransport] = None,
        runner: packages.Runner = packages.run,
        venv_list: Callable[[], tuple[Venv, ...]] = packages.venvs,
    ) -> None:
        self.llama_dir = llama_dir
        self.previous_dir = llama_dir.with_name(llama_dir.name + ".previous")
        self.staged_dir = llama_dir.with_name(llama_dir.name + ".staged")
        self.downloads_dir = downloads_dir
        self.state_path = state_path
        self._release_all = release_all
        self._release_voice = release_voice
        self._relaunch = relaunch
        self._transport = transport
        self._runner = runner
        self._venvs = venv_list
        self._job: Optional[UpdateJob] = None
        self._task: Optional[asyncio.Task] = None
        self._latest: dict[str, Any] = {}
        self._checked_at: Optional[float] = None
        self._build_cache: dict[str, tuple[float, Optional[dict]]] = {}
        self._state = self._load()
        # Set unless queued installs for the backend's own venv still have to run (see apply_pending).
        self.packages_ready = threading.Event()
        if not self._live_pending():
            self.packages_ready.set()

    # --- state ---------------------------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {"pending": data.get("pending") or {}, "rollback": data.get("rollback") or {},
                        "last": data.get("last") or {}}
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.warning("Ignoring unreadable %s: %s", self.state_path, exc)
        return {"pending": {}, "rollback": {}, "last": {}}

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def _record(self, tool: str, ok: bool, message: str) -> None:
        self._state["last"][tool] = {"at": time.time(), "ok": ok, "message": message}
        self._save()

    def _live_pending(self) -> list[str]:
        live = {v.id for v in self._venvs() if v.live}
        return [k for k in self._state["pending"] if k in live]

    # --- status --------------------------------------------------------------------------------

    def _client(self, timeout: float = 30.0) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=_HEADERS, transport=self._transport)

    def _build_of(self, folder: Path) -> Optional[dict[str, Any]]:
        exe = folder / llama_cpp.SERVER_EXE
        mtime = exe.stat().st_mtime if exe.is_file() else -1.0
        cached = self._build_cache.get(str(folder))
        if cached and cached[0] == mtime:
            return cached[1]
        build = llama_cpp.installed_build(folder) if mtime >= 0 else None
        self._build_cache[str(folder)] = (mtime, build)
        return build

    async def check(self) -> None:
        """Asks GitHub and PyPI what is newest. Failures land in the tool's ``error``, not here."""
        latest: dict[str, Any] = {}
        async with self._client() as client:
            try:
                r = await client.get(llama_cpp.RELEASES_URL)
                r.raise_for_status()
                major = await asyncio.to_thread(llama_cpp.cuda_major, self.llama_dir)
                latest[LLAMA_ID] = llama_cpp.pick_release(r.json(), major) or "No matching Windows build was published."
            except Exception as exc:
                latest[LLAMA_ID] = f"Couldn't reach GitHub: {exc}"

            async def pypi(name: str) -> tuple[str, Optional[str]]:
                try:
                    r = await client.get(PYPI_URL.format(name=name))
                    r.raise_for_status()
                    return name, r.json()["info"]["version"]
                except Exception:
                    return name, None

            names = sorted({p for v in self._venvs() for p in v.packages})
            latest["pypi"] = dict(await asyncio.gather(*(pypi(n) for n in names)))
        self._latest, self._checked_at = latest, time.time()

    async def status(self, check: bool = False) -> dict[str, Any]:
        if check or self._checked_at is None:
            await self.check()
        tools = [await self._llama_status()]
        for venv in self._venvs():
            tools.append(await self._venv_status(venv))
        return {"tools": tools, "job": self._job.to_dict() if self._job else None, "checked_at": self._checked_at}

    async def _llama_status(self) -> dict[str, Any]:
        current = await asyncio.to_thread(self._build_of, self.llama_dir)
        previous = await asyncio.to_thread(self._build_of, self.previous_dir) if self.previous_dir.is_dir() else None
        latest = self._latest.get(LLAMA_ID)
        release = latest if isinstance(latest, llama_cpp.Release) else None
        return {
            "id": LLAMA_ID, "kind": "llama", "name": "llama.cpp",
            "detail": "Runs the chat model, the image captioner and the embeddings.",
            "installed": f"build {current['build']}" if current else None,
            "latest": f"build {release.build}" if release else None,
            "update_available": bool(release and current and release.build > current["build"]),
            "download_size": release.size if release else None,
            "release_page": release.page if release else None,
            "roll_back_to": f"build {previous['build']}" if previous else None,
            "pending": None, "packages": [],
            "error": latest if isinstance(latest, str) else (None if current else f"No {llama_cpp.SERVER_EXE} in {self.llama_dir}"),
            "last": self._state["last"].get(LLAMA_ID),
        }

    async def _venv_status(self, venv: Venv) -> dict[str, Any]:
        error = None
        try:
            have = await asyncio.to_thread(packages.installed, venv, self._runner)
        except Exception as exc:
            have, error = {}, str(exc)
        pypi = self._latest.get("pypi") or {}
        rows = []
        for name in venv.packages:
            cur, new = have.get(packages.normalise(name)), pypi.get(name)
            rows.append({"name": name, "installed": cur, "latest": new, "held": False,
                         "outdated": bool(cur and new and _newer(new, cur))})
        for name in venv.held:
            cur = have.get(packages.normalise(name))
            if cur:
                rows.append({"name": name, "installed": cur, "latest": None, "held": True, "outdated": False})
        pending = self._state["pending"].get(venv.id)
        rollback = self._state["rollback"].get(venv.id) or {}
        return {
            "id": venv.id, "kind": "packages", "name": venv.name, "detail": venv.detail,
            "installed": None, "latest": None,
            "update_available": any(r["outdated"] for r in rows) and not pending,
            "download_size": None, "release_page": None,
            "roll_back_to": ", ".join(f"{n} {v}" for n, v in sorted(rollback.items())) or None,
            "pending": (("Roll back" if pending["action"] == "rollback" else "Update") + " installs when Jarvis next starts.")
            if pending else None,
            "packages": rows, "error": error, "last": self._state["last"].get(venv.id),
        }

    # --- jobs ----------------------------------------------------------------------------------

    @property
    def job(self) -> Optional[UpdateJob]:
        return self._job

    def start(self, tool: str, action: str) -> UpdateJob:
        if self._job and self._job.status in ACTIVE:
            raise RuntimeError("Another update is still running.")
        if action not in ("update", "rollback"):
            raise ValueError(f"Unknown action {action!r}")
        venv = next((v for v in self._venvs() if v.id == tool), None)
        if tool != LLAMA_ID and venv is None:
            raise KeyError(tool)
        reason = blocked()
        if reason:
            raise RuntimeError(reason)
        job = UpdateJob(id=uuid.uuid4().hex[:12], tool=tool, action=action)
        self._job = job
        work = self._llama(job) if tool == LLAMA_ID else self._venv(job, venv)
        self._task = asyncio.create_task(self._run(job, work))
        return job

    async def wait(self) -> None:
        if self._task:
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def cancel(self) -> bool:
        """Stops a download. Once files are being swapped the job runs to the end."""
        if not self._job or self._job.status not in ACTIVE or self._job.stage != "Downloading" or not self._task:
            return False
        self._task.cancel()
        return True

    def cancel_pending(self, tool: str) -> bool:
        if self._state["pending"].pop(tool, None) is None:
            return False
        self._save()
        return True

    async def _run(self, job: UpdateJob, work: Awaitable[str]) -> None:
        try:
            job.message = await work
            job.status = "done"
            self._record(job.tool, True, job.message)
        except asyncio.CancelledError:
            job.status, job.message = "cancelled", "Cancelled."
        except Exception as exc:
            logger.warning("Tool update %s (%s %s) failed: %s", job.id, job.action, job.tool, exc)
            job.status, job.message = "failed", str(exc)
            self._record(job.tool, False, job.message)
        finally:
            job.finished_at = time.time()

    # --- llama.cpp -----------------------------------------------------------------------------

    async def _llama(self, job: UpdateJob) -> str:
        if job.action == "rollback":
            before = await asyncio.to_thread(self._build_of, self.llama_dir)
            if not self.previous_dir.is_dir():
                raise RuntimeError("There is no previous build to go back to.")
            await self._swap(job, lambda: llama_cpp.swap_back(self.llama_dir, self.previous_dir),
                             undo=lambda: llama_cpp.swap_back(self.llama_dir, self.previous_dir))
            after = await asyncio.to_thread(self._build_of, self.llama_dir)
            return f"Back on build {after['build'] if after else '?'} (build {before['build'] if before else '?'} kept to switch back)."

        if not isinstance(self._latest.get(LLAMA_ID), llama_cpp.Release):
            job.stage = "Checking"
            await self.check()
        release = self._latest.get(LLAMA_ID)
        if not isinstance(release, llama_cpp.Release):
            raise RuntimeError(release or "No llama.cpp release found.")
        current = await asyncio.to_thread(self._build_of, self.llama_dir)
        if current and release.build <= current["build"]:
            return f"Already on build {current['build']}."

        zip_path = self.downloads_dir / release.asset
        await self._download(job, release, zip_path)
        job.stage = "Unpacking"
        await asyncio.to_thread(llama_cpp.extract, zip_path, self.staged_dir)
        await asyncio.to_thread(llama_cpp.carry_runtime, self.llama_dir, self.staged_dir)
        got = await asyncio.to_thread(llama_cpp.installed_build, self.staged_dir)
        if not got or got["build"] != release.build:
            raise RuntimeError(f"The downloaded build doesn't run here (reported {got}).")
        await self._swap(job, lambda: llama_cpp.swap_in(self.llama_dir, self.staged_dir, self.previous_dir),
                         undo=lambda: llama_cpp.swap_back(self.llama_dir, self.previous_dir))
        zip_path.unlink(missing_ok=True)
        was = f" (was build {current['build']})" if current else ""
        return f"Updated to build {release.build}{was}; the chat model started on it."

    async def _download(self, job: UpdateJob, release: llama_cpp.Release, dest: Path) -> None:
        job.stage, job.bytes_total, job.bytes_done = "Downloading", release.size, 0
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_suffix(dest.suffix + ".part")
        digest = hashlib.sha256()
        async with self._client(timeout=60.0) as client:
            async with client.stream("GET", release.url) as r:
                r.raise_for_status()
                with open(part, "wb") as out:
                    async for chunk in r.aiter_bytes(1024 * 1024):
                        out.write(chunk)
                        digest.update(chunk)
                        job.bytes_done += len(chunk)
        if release.sha256 and digest.hexdigest() != release.sha256:
            part.unlink(missing_ok=True)
            raise RuntimeError("The download didn't match GitHub's checksum; nothing was changed.")
        os.replace(part, dest)

    async def _swap(self, job: UpdateJob, do: Callable[[], None], undo: Callable[[], None]) -> None:
        job.stage = "Stopping the model"
        try:
            with hold(HOLD_KEY, "llama.cpp is being updated; the model is back in a moment."):
                await self._release_all()
                job.stage = "Swapping files"
                await asyncio.to_thread(do)
        except OSError as exc:
            # The swap functions leave the old build in place when a rename fails.
            await self._relaunch()
            raise RuntimeError(f"Couldn't swap the files (something still had one open): {exc}") from exc
        job.stage = "Starting the model"
        if await self._relaunch():
            return
        with hold(HOLD_KEY, "llama.cpp is being restored; the model is back in a moment."):
            await self._release_all()
            await asyncio.to_thread(undo)
        await self._relaunch()
        raise RuntimeError("The chat model didn't start on that build, so the previous one is back.")

    # --- packages ------------------------------------------------------------------------------

    def _pins_for(self, venv: Venv, action: str) -> dict[str, str]:
        if action == "rollback":
            pins = dict(self._state["rollback"].get(venv.id) or {})
            if not pins:
                raise RuntimeError("Nothing to roll back to.")
            return pins
        pypi = self._latest.get("pypi") or {}
        have = packages.installed(venv, self._runner)
        pins = {n: pypi[n] for n in venv.packages
                if pypi.get(n) and have.get(packages.normalise(n)) and _newer(pypi[n], have[packages.normalise(n)])}
        if not pins:
            raise RuntimeError("Everything is up to date.")
        return pins

    async def _venv(self, job: UpdateJob, venv: Venv) -> str:
        if job.action == "update" and not self._latest.get("pypi"):
            job.stage = "Checking"
            await self.check()
        pins = await asyncio.to_thread(self._pins_for, venv, job.action)
        if venv.live:
            self._state["pending"][venv.id] = {"action": job.action, "pins": pins}
            self._save()
            return "Installs when Jarvis next starts: " + _describe(pins) + "."
        job.stage = "Stopping Bark and Laya"
        with hold(HOLD_KEY, "Voice packages are being updated."):
            await self._release_voice()
            job.stage = "Installing"
            result = await asyncio.to_thread(self._install, venv, pins)
        return "Installed " + (_describe_changes(result["changed"]) or "nothing new") + "."

    def _install(self, venv: Venv, pins: dict[str, str]) -> dict[str, Any]:
        result = packages.install(venv, pins, self.downloads_dir, self._runner)
        if result["rollback"]:
            self._state["rollback"][venv.id] = result["rollback"]
            self._save()
        return result

    def apply_pending(self) -> None:
        """Runs at startup on a worker thread: the queued installs for the backend's own venv, before
        Whisper or Kokoro is imported (they wait on ``packages_ready``)."""
        try:
            for venv in self._venvs():
                entry = self._state["pending"].get(venv.id)
                if not venv.live or not entry:
                    continue
                self._state["pending"].pop(venv.id, None)
                self._save()
                try:
                    result = self._install(venv, entry["pins"])
                    message = "Installed " + (_describe_changes(result["changed"]) or "nothing new") + "."
                    self._record(venv.id, True, message)
                    logger.info("Tool updates (%s): %s", venv.id, message)
                except Exception as exc:
                    self._record(venv.id, False, str(exc))
                    logger.warning("Tool updates (%s) failed at startup: %s", venv.id, exc)
        finally:
            self.packages_ready.set()


def _newer(a: str, b: str) -> bool:
    try:
        return Version(a) > Version(b)
    except InvalidVersion:
        return a != b


def _describe(pins: dict[str, str]) -> str:
    return ", ".join(f"{n} {v}" for n, v in sorted(pins.items()))


def _describe_changes(changed: dict[str, list[str]]) -> str:
    return ", ".join(f"{n} {old} -> {new}" for n, (old, new) in sorted(changed.items()))


_manager: Optional[UpdateManager] = None


def configure_update_manager(manager: UpdateManager) -> UpdateManager:
    global _manager
    _manager = manager
    return manager


def get_update_manager() -> Optional[UpdateManager]:
    return _manager


def wait_for_packages(timeout: float = 600.0) -> None:
    """Called before importing faster-whisper or Kokoro: returns once queued installs are done."""
    manager = _manager
    if manager is not None and not manager.packages_ready.is_set():
        logger.info("Waiting for queued voice package installs before loading voice...")
        manager.packages_ready.wait(timeout)
