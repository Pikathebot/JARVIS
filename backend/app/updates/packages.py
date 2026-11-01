"""
Python packages behind voice, per virtualenv: what is installed, what PyPI has, and how to move.

Only the packages named here are ever upgraded, each to an exact version. The CUDA builds of torch
(``2.11.0+cu128``) are *held*: passed as constraints at their installed version, because a plain
upgrade from PyPI would swap in the CPU build -- an update that needs a newer torch fails instead.

The backend's own venv (Whisper, Kokoro) cannot be changed while the backend has those packages
loaded (Windows locks their DLLs, and Kokoro warms up at every start), so its updates are queued
and installed at the next start before anything imports them (``app.updates.manager``).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from app.agent.model_catalog import REPO_ROOT

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
INSTALL_TIMEOUT_S = 900


@dataclass(frozen=True)
class Venv:
    id: str
    name: str
    detail: str
    python: Path
    installer: str  # "pip" | "uv"
    packages: tuple[str, ...]
    held: tuple[str, ...] = ()
    live: bool = False
    """The backend's own environment: changes are queued for the next start."""


def venvs() -> tuple[Venv, ...]:
    return (
        Venv("voice", "Whisper and Kokoro", "Speech to text fallback and the speaking voice, in the backend.",
             Path(sys.executable), "pip", ("faster-whisper", "ctranslate2", "kokoro-onnx", "onnxruntime"),
             live=True),
        Venv("voice-sidecars", "Bark and Laya", "Main speech to text and the voice-mode router (tools/laya-venv).",
             REPO_ROOT / "tools" / "laya-venv" / "Scripts" / "python.exe", "uv",
             ("laya", "transformers"), held=("torch", "torchaudio", "torchvision")),
    )



def normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


Runner = Callable[[list[str], float], subprocess.CompletedProcess]


def run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=_NO_WINDOW)


def uv_exe() -> Optional[str]:
    return shutil.which("uv")


def list_command(venv: Venv) -> Optional[list[str]]:
    if venv.installer == "uv":
        uv = uv_exe()
        return [uv, "pip", "list", "--python", str(venv.python), "--format", "json"] if uv else None
    return [str(venv.python), "-m", "pip", "list", "--format", "json", "--disable-pip-version-check"]


def installed(venv: Venv, runner: Runner = run) -> dict[str, str]:
    """Every package in the venv, normalised name -> version. Raises RuntimeError when it can't be read."""
    if not venv.python.is_file():
        raise RuntimeError(f"{venv.python} is missing")
    cmd = list_command(venv)
    if cmd is None:
        raise RuntimeError("uv is not on PATH")
    out = runner(cmd, 60)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout or "").strip()[-400:] or "package list failed")
    return {normalise(p["name"]): p["version"] for p in json.loads(out.stdout or "[]")}


def install_command(venv: Venv, pins: dict[str, str], constraints: Path) -> list[str]:
    specs = [f"{name}=={version}" for name, version in sorted(pins.items())]
    if venv.installer == "uv":
        return [uv_exe() or "uv", "pip", "install", "--python", str(venv.python), *specs, "-c", str(constraints)]
    return [str(venv.python), "-m", "pip", "install", "--disable-pip-version-check", *specs, "-c", str(constraints)]


def constraint_lines(venv: Venv, before: dict[str, str]) -> list[str]:
    return [f"{name}=={before[normalise(name)]}" for name in venv.held if normalise(name) in before]


def install(venv: Venv, pins: dict[str, str], workdir: Path, runner: Runner = run) -> dict[str, dict[str, str]]:
    """
    Installs ``pins`` (name -> exact version) with the held packages fixed. Returns
    ``{"changed": {name: [old, new]}, "rollback": {name: old}}`` -- every package whose version
    moved, including dependencies -- so a roll back can put each one back.
    """
    before = installed(venv, runner)
    workdir.mkdir(parents=True, exist_ok=True)
    constraints = workdir / f"{venv.id}-constraints.txt"
    constraints.write_text("\n".join(constraint_lines(venv, before)) + "\n", encoding="utf-8")
    out = runner(install_command(venv, pins, constraints), INSTALL_TIMEOUT_S)
    if out.returncode != 0:
        raise RuntimeError(_tail(out))
    after = installed(venv, runner)
    changed = {n: [before[n], v] for n, v in after.items() if n in before and before[n] != v}
    return {"changed": changed, "rollback": {n: old for n, (old, _new) in changed.items()}}


def _tail(out: subprocess.CompletedProcess, limit: int = 600) -> str:
    text = ((out.stderr or "") + "\n" + (out.stdout or "")).strip()
    return text[-limit:] or f"installer exited with {out.returncode}"
