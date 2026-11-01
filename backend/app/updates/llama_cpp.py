"""
llama.cpp builds: which one is installed, which is the newest on GitHub, and swapping one in.

ggml-org publishes every build as a prerelease tagged ``b<N>`` with zips per backend; the Windows
CUDA one carries no CUDA runtime (that is a separate ~390 MB zip), so the runtime DLLs of the
install being replaced are copied across instead -- as long as the CUDA major matches, which the
release is picked by. The old folder is kept as ``<dir>.previous`` for a roll back.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

RELEASES_URL = "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=20"
SERVER_EXE = "llama-server.exe"
# "version: 0.7.0-dev (build 11899, commit c6fb3b5be)"; builds before ~11000: "version: 10689 (3f2c1a)".
_VERSION_RXS = (re.compile(r"build (\d+), commit ([0-9a-f]+)"), re.compile(r"version: (\d+) \(([0-9a-f]+)\)"))
_CUDART_RX = re.compile(r"cudart64_(\d+)\.dll$", re.I)
# The CUDA runtime DLLs the cudart zip would bring; carried over from the old install.
_RUNTIME_GLOBS = ("cudart64_*.dll", "cublas64_*.dll", "cublasLt64_*.dll")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass(frozen=True)
class Release:
    build: int
    asset: str
    url: str
    size: int
    sha256: Optional[str]
    published_at: str
    page: str

    def to_dict(self) -> dict[str, Any]:
        return {"build": self.build, "asset": self.asset, "size": self.size,
                "published_at": self.published_at, "page": self.page}


def parse_version(text: str) -> Optional[dict[str, Any]]:
    for rx in _VERSION_RXS:
        m = rx.search(text or "")
        if m:
            return {"build": int(m.group(1)), "commit": m.group(2)}
    return None


def installed_build(folder: Path) -> Optional[dict[str, Any]]:
    """``{build, commit}`` of the llama-server in ``folder``, or None when there is none or it won't run."""
    exe = folder / SERVER_EXE
    if not exe.is_file():
        return None
    try:
        out = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=20,
                             creationflags=_NO_WINDOW, cwd=str(folder))
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("Could not read the llama.cpp version from %s: %s", exe, exc)
        return None
    return parse_version((out.stdout or "") + (out.stderr or ""))


def cuda_major(folder: Path) -> Optional[int]:
    """12 for an install carrying cudart64_12.dll; None for a CPU/Vulkan build."""
    for dll in folder.glob("cudart64_*.dll"):
        m = _CUDART_RX.search(dll.name)
        if m:
            return int(m.group(1))
    return None


def pick_release(releases: list[dict[str, Any]], major: Optional[int]) -> Optional[Release]:
    """The newest build with a Windows x64 zip for this CUDA major (CPU build when ``major`` is None)."""
    flavour = rf"cuda-{major}\.[\d.]+" if major else "cpu"
    best: Optional[Release] = None
    for rel in releases:
        if rel.get("draft"):
            continue
        tag = re.fullmatch(r"b(\d+)", rel.get("tag_name") or "")
        if not tag:
            continue
        build = int(tag.group(1))
        rx = re.compile(rf"llama-b{build}-bin-win-{flavour}-x64\.zip")
        for asset in rel.get("assets") or []:
            if rx.fullmatch(asset.get("name") or "") and (best is None or build > best.build):
                digest = asset.get("digest") or ""
                best = Release(
                    build=build, asset=asset["name"], url=asset["browser_download_url"],
                    size=int(asset.get("size") or 0),
                    sha256=digest.split(":", 1)[1] if digest.startswith("sha256:") else None,
                    published_at=rel.get("published_at") or "", page=rel.get("html_url") or "",
                )
    return best


def extract(zip_path: Path, dest: Path) -> None:
    """Unzip into ``dest`` (fresh), refusing any entry that would land outside it. A zip whose files
    all sit in one top folder is flattened, so llama-server.exe ends up directly in ``dest``."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    root = dest.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            target = (dest / name).resolve()
            if target != root and root not in target.parents:
                raise ValueError(f"Unsafe path in {zip_path.name}: {name!r}")
        zf.extractall(dest)
    entries = list(dest.iterdir())
    if len(entries) == 1 and entries[0].is_dir() and not (dest / SERVER_EXE).exists():
        inner = entries[0]
        for child in inner.iterdir():
            child.rename(dest / child.name)
        inner.rmdir()


def carry_runtime(old: Path, new: Path) -> list[str]:
    """Copy the CUDA runtime DLLs the new build lacks from the old install. Returns what was copied."""
    copied = []
    for pattern in _RUNTIME_GLOBS:
        for dll in old.glob(pattern):
            if not (new / dll.name).exists():
                shutil.copy2(dll, new / dll.name)
                copied.append(dll.name)
    return copied


def swap_in(current: Path, staged: Path, previous: Path, attempts: int = 5) -> None:
    """``current`` -> ``previous`` and ``staged`` -> ``current``. Retries while Windows still holds a
    handle from a process that was just stopped; puts ``current`` back if the second step fails."""
    if previous.exists():
        shutil.rmtree(previous)
    _rename(current, previous, attempts)
    try:
        _rename(staged, current, attempts)
    except OSError:
        _rename(previous, current, attempts)
        raise


def swap_back(current: Path, previous: Path, attempts: int = 5) -> None:
    """Exchanges ``current`` and ``previous``, so a roll back can itself be rolled back."""
    spare = current.with_name(current.name + ".swap")
    if spare.exists():
        shutil.rmtree(spare)
    _rename(current, spare, attempts)
    try:
        _rename(previous, current, attempts)
    except OSError:
        _rename(spare, current, attempts)
        raise
    _rename(spare, previous, attempts)


def _rename(src: Path, dst: Path, attempts: int) -> None:
    for attempt in range(attempts):
        try:
            src.rename(dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(1.0)
