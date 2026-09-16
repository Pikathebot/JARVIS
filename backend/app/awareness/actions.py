"""
Proactive follow-ups the awareness monitor can attach to an observation.

The monitor announces; an action here is what Jarvis *does* about it once per streak (see
``AwarenessMonitor.actions``). Two of them are read-only lookups whose result is worth saying
out loud: which external app is squeezing the GPU and by how much, and what is actually taking
up the disk when it runs out. Both are best-effort -- a missing NVML or an unreadable folder
just means fewer numbers, never a failure that stops the announcement.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Iterable, Optional

import psutil

logger = logging.getLogger("jarvis.awareness.actions")


# ------------------------------------------------------------ heavy apps

def _gpu_memory_by_pid_pdh() -> dict[int, float]:
    """
    Per-process dedicated GPU memory in MB from the Windows ``GPU Process Memory`` counter.
    This is what Task Manager shows; on WDDM it is the only source that works, since NVML
    reports every process's memory as N/A there.
    """
    import re

    import win32pdh  # type: ignore

    usage: dict[int, float] = {}
    query = win32pdh.OpenQuery()
    try:
        counter = win32pdh.AddEnglishCounter(query, r"\GPU Process Memory(*)\Dedicated Usage")
        win32pdh.CollectQueryData(query)
        for instance, value in win32pdh.GetFormattedCounterArray(counter, win32pdh.PDH_FMT_LARGE).items():
            match = re.match(r"pid_(\d+)_", instance)
            if not match or not value:
                continue
            pid = int(match.group(1))
            usage[pid] = usage.get(pid, 0.0) + value / (1024 * 1024)
    finally:
        win32pdh.CloseQuery(query)
    return usage


def _gpu_memory_by_pid_nvml() -> dict[int, float]:
    """Per-process GPU memory in MB from NVML (Linux, or Windows in TCC mode)."""
    try:
        import nvidia_ml_py as pynvml  # type: ignore
    except ImportError:
        import pynvml  # type: ignore
    pynvml.nvmlInit()
    try:
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        usage: dict[int, float] = {}
        for getter in ("nvmlDeviceGetGraphicsRunningProcesses", "nvmlDeviceGetComputeRunningProcesses"):
            fn = getattr(pynvml, getter, None)
            if fn is None:
                continue
            try:
                for proc in fn(handle):
                    mem = getattr(proc, "usedGpuMemory", None)
                    if mem is None or mem < 0:
                        continue
                    usage[int(proc.pid)] = max(usage.get(int(proc.pid), 0.0), mem / (1024 * 1024))
            except Exception:
                continue
        return usage
    finally:
        try:
            pynvml.nvmlShutdown()
        except Exception:
            pass


def _gpu_memory_by_pid(cap_mb: Optional[float] = None) -> dict[int, float]:
    """
    Per-process GPU memory in MB, or {} when it cannot be read. ``cap_mb`` (the card's
    total) discards readings that cannot be real -- the NVIDIA overlay's counter is known
    to report tens of gigabytes on an 8 GB card.
    """
    usage: dict[int, float] = {}
    for reader in ((_gpu_memory_by_pid_pdh,) if os.name == "nt" else ()) + (_gpu_memory_by_pid_nvml,):
        try:
            usage = reader()
        except Exception as e:
            logger.debug("Per-process GPU memory via %s unavailable: %s", reader.__name__, e)
            continue
        if usage:
            break
    if cap_mb and cap_mb > 0:
        usage = {pid: mb for pid, mb in usage.items() if mb <= cap_mb}
    return usage


def heavy_app_stats(
    labels: Iterable[str], watchlist: Iterable[Any], vram_total_mb: Optional[float] = None
) -> dict[str, dict[str, float]]:
    """
    Resource use of the confirmed heavy apps, keyed by their watchlist label:
    ``{"Blender": {"vram_mb": 2100.0, "ram_mb": 3400.0, "cpu_percent": 35.0}}``.

    An app is usually several processes (Chrome, Unreal); each label sums the ones its
    watchlist rule matches. ``labels`` is what the governor confirmed, ``watchlist`` the rules
    the process watcher used to find them -- the same rules re-applied here so the numbers
    belong to the same processes.
    """
    wanted = set(labels)
    if not wanted:
        return {}
    rules = [r for r in watchlist if getattr(r, "label", None) in wanted]
    if not rules:
        return {}

    gpu_by_pid = _gpu_memory_by_pid(cap_mb=vram_total_mb)
    stats: dict[str, dict[str, float]] = {}
    try:
        for proc in psutil.process_iter(["pid", "name", "memory_info"]):
            try:
                name = proc.info.get("name") or ""
                for rule in rules:
                    if not rule.matches(name):
                        continue
                    entry = stats.setdefault(rule.label, {"vram_mb": 0.0, "ram_mb": 0.0, "cpu_percent": 0.0})
                    mem = proc.info.get("memory_info")
                    if mem is not None:
                        entry["ram_mb"] += mem.rss / (1024 * 1024)
                    entry["vram_mb"] += gpu_by_pid.get(proc.info["pid"], 0.0)
                    try:
                        entry["cpu_percent"] += proc.cpu_percent(interval=None)
                    except Exception:
                        pass
                    break
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
    except Exception as e:
        logger.debug("Heavy app stats unavailable: %s", e)
    return stats


# ------------------------------------------------------------- disk space

def largest_files(roots: Iterable[Path], top_n: int = 5, max_files: int = 200_000) -> list[tuple[Path, int]]:
    """
    The biggest files under the given roots, largest first. Walks with ``os.scandir`` and
    stops after ``max_files`` entries so a runaway tree cannot pin the CPU for minutes.
    """
    found: list[tuple[Path, int]] = []
    seen = 0
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        stack = [root]
        while stack and seen < max_files:
            current = stack.pop()
            try:
                with os.scandir(current) as it:
                    for entry in it:
                        seen += 1
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(Path(entry.path))
                            elif entry.is_file(follow_symlinks=False):
                                found.append((Path(entry.path), entry.stat(follow_symlinks=False).st_size))
                        except OSError:
                            continue
            except OSError:
                continue
    found.sort(key=lambda item: item[1], reverse=True)
    return found[:top_n]


def _human_size(size_bytes: int) -> str:
    gb = size_bytes / (1024 ** 3)
    if gb >= 1.0:
        return f"{gb:.1f} gigabytes"
    mb = size_bytes / (1024 ** 2)
    return f"{mb:.0f} megabytes"


def _describe_file(path: Path, roots: Iterable[Path]) -> str:
    """'projects/abc/artifacts/report.pdf' rather than the full absolute path."""
    for root in roots:
        try:
            rel = path.resolve().relative_to(Path(root).resolve())
            return f"{Path(root).name}/{rel.as_posix()}"
        except (ValueError, OSError):
            continue
    return path.name


def describe_disk_offenders(roots: Iterable[Path], top_n: int = 5) -> Optional[str]:
    """
    Speakable summary of what is taking the space, e.g. "The largest things I manage are
    models/qwen3.5-9b/Qwen3.5-9B-UD-Q3_K_XL.gguf at 5.0 gigabytes, ...". None when nothing
    is found, so the caller stays quiet rather than announcing an empty list.
    """
    roots = [Path(r) for r in roots]
    started = time.time()
    biggest = largest_files(roots, top_n=top_n)
    logger.info("Disk offender scan over %s took %.1fs", [str(r) for r in roots], time.time() - started)
    if not biggest:
        return None
    parts = [f"{_describe_file(p, roots)} at {_human_size(size)}" for p, size in biggest]
    if len(parts) == 1:
        listing = parts[0]
    else:
        listing = ", ".join(parts[:-1]) + f", and {parts[-1]}"
    return f"The largest things I manage are {listing}. Removing what you no longer need would free the most space."
