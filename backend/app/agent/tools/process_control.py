import logging
import sys
import time
from typing import Optional, Any
import psutil

logger = logging.getLogger("jarvis.agent.tools.process_control")

# psutil's first cpu_percent() per process is always 0.0 (it measures from the previous call),
# which is why every process showed 0% CPU: prime them all, wait this long, read again.
_CPU_SAMPLE_SECONDS = 0.5
_CLOSE_WAIT_SECONDS = 5.0


def list_processes(filter_name: Optional[str] = None, sort_by: str = "memory", group: bool = True) -> str:
    """
    List running programs, one row per app with its process count, memory and CPU % (share of the whole CPU, as Task Manager shows it); sort_by="cpu" when the question is about CPU use, group=false for one row per PID.

    Args:
        filter_name: Optional process name substring to filter results (case-insensitive).
        sort_by: "memory" (default) or "cpu".
        group: One row per app name (default) or one per process.
    """
    filter_clean = (filter_name or "").strip().lower()
    procs: list[psutil.Process] = []
    try:
        for proc in psutil.process_iter(['pid', 'name']):
            name = proc.info.get('name') or ""
            if proc.pid == 0:
                continue  # "System Idle Process": its CPU % is idle time, not load
            if filter_clean and filter_clean not in name.lower():
                continue
            try:
                proc.cpu_percent(None)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass
            procs.append(proc)
    except Exception as e:
        logger.error("Error listing processes: %s", e)
        return f"Error listing processes: {str(e)}"

    time.sleep(_CPU_SAMPLE_SECONDS)
    cores = psutil.cpu_count() or 1
    results: list[dict[str, Any]] = []
    for proc in procs:
        name = proc.info.get('name') or "unknown"
        try:
            with proc.oneshot():
                mem_mb = round(proc.memory_info().rss / (1024 * 1024), 1)
                cpu = proc.cpu_percent(None) / cores
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except psutil.AccessDenied:
            mem_mb, cpu = 0.0, 0.0
        results.append({"pid": proc.pid, "name": name, "cpu_percent": cpu, "memory_mb": mem_mb})

    if not results:
        if filter_clean:
            return f"No running processes found matching '{filter_name}'."
        return "No running processes found."

    key = "cpu_percent" if str(sort_by or "").strip().lower() == "cpu" else "memory_mb"
    if group:
        apps: dict[str, dict[str, Any]] = {}
        for r in results:
            app = apps.setdefault(r["name"].lower(), {"name": r["name"], "count": 0, "cpu_percent": 0.0, "memory_mb": 0.0})
            app["count"] += 1
            app["cpu_percent"] += r["cpu_percent"]
            app["memory_mb"] += r["memory_mb"]
        rows = sorted(apps.values(), key=lambda x: x[key], reverse=True)
        lines = [f"{len(rows)} apps, {len(results)} processes (sorted by {'CPU' if key == 'cpu_percent' else 'memory'}):"]
        for a in rows[:40]:
            mem = f"{a['memory_mb'] / 1024:.1f} GB" if a["memory_mb"] >= 1024 else f"{a['memory_mb']:.0f} MB"
            count = f" x{a['count']}" if a["count"] > 1 else ""
            lines.append(f"- {a['name']}{count}: {mem}, {a['cpu_percent']:.1f}% CPU")
        if len(rows) > 40:
            lines.append(f"... and {len(rows) - 40} more apps.")
        return "\n".join(lines)

    results.sort(key=lambda x: x[key], reverse=True)

    lines = [f"{'PID':<8} {'Name':<35} {'Memory (MB)':<14} {'CPU %':<8}", "-" * 68]
    for r in results[:50]:  # Cap at 50 to prevent excessive token output
        lines.append(f"{r['pid']:<8} {r['name']:<35} {r['memory_mb']:<14.1f} {r['cpu_percent']:<8.1f}")

    if len(results) > 50:
        lines.append(f"... and {len(results) - 50} more processes.")

    return "\n".join(lines)


def jarvis_process_ids() -> set[int]:
    """Every process that is part of Jarvis: this backend, the uvicorn reloader above it, the
    Jarvis.App client, and the llama-servers (chat model, captioner and embedding sidecars) and
    Python workers below them. kill_process refuses all of them. Descendants are filtered by name:
    apps that launch_app started can be children of the backend, and those stay closable."""
    me = psutil.Process()
    roots = [me]
    try:
        for parent in me.parents():
            pname = (parent.name() or "").lower()
            if pname.startswith(("python", "uvicorn")) or pname == "jarvis.app.exe":
                roots.append(parent)
            else:
                break
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    try:
        roots += [p for p in psutil.process_iter(['name']) if (p.info.get('name') or "").lower() == "jarvis.app.exe"]
    except Exception:
        pass
    protected: set[int] = set()
    for root in roots:
        protected.add(root.pid)
        try:
            for child in root.children(recursive=True):
                try:
                    cname = (child.name() or "").lower()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
                if cname == "llama-server.exe" or cname.startswith(("python", "uvicorn")):
                    protected.add(child.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return protected


def _close_windows(pids: set[int]) -> int:
    """Post WM_CLOSE to the visible top-level windows of these processes -- what clicking X
    does, so the app can ask to save. Returns how many windows were asked."""
    if sys.platform != "win32" or not pids:
        return 0
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    WM_CLOSE, GW_OWNER = 0x0010, 4
    asked = 0

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _each(hwnd, _):
        nonlocal asked
        if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, GW_OWNER):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids:
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            asked += 1
        return True

    user32.EnumWindows(_each, 0)
    return asked


def kill_process(pid_or_name: str, force: bool = False) -> str:
    """
    Close a running program by PID or process name the way clicking its X does (it may ask to save); force=true ends it at once and loses unsaved work -- only when the user says force, or it is frozen.

    Args:
        pid_or_name: Target process PID (e.g. '1234') or process name (e.g. 'notepad.exe', 'notepad').
        force: End the process immediately instead of asking it to close.
    """
    target = str(pid_or_name or "").strip()
    if not target:
        return "Error: No process PID or name provided to close."

    protected = jarvis_process_ids()
    procs: list[psutil.Process] = []
    skipped_self = 0

    if target.isdigit():
        if int(target) in protected:
            logger.warning("Blocked attempt to close a Jarvis process (PID %s)", target)
            return f"Error: PID {target} is part of Jarvis itself; it can't be closed from chat."
        try:
            procs.append(psutil.Process(int(target)))
        except psutil.NoSuchProcess:
            return f"Error: No running process found with PID {target}."
        except psutil.AccessDenied:
            return f"Error: Access denied accessing process with PID {target}."
    else:
        target_lower = target.lower()
        target_exe = target_lower if target_lower.endswith(".exe") else f"{target_lower}.exe"
        for p in psutil.process_iter(['pid', 'name']):
            if (p.info.get('name') or "").lower() in (target_lower, target_exe):
                if p.pid in protected:
                    skipped_self += 1
                    continue
                procs.append(p)
        if not procs:
            if skipped_self:
                return f"Error: '{target}' is part of Jarvis itself; it can't be closed from chat."
            return f"Error: No running processes found matching '{target}'."

    name = target
    try:
        name = procs[0].name()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

    if not force:
        if _close_windows({p.pid for p in procs}) == 0:
            return (f"'{name}' has no open window to close, so it can only be ended by force "
                    "(force=true), which loses anything unsaved. Ask the user first.")
        gone, alive = psutil.wait_procs(procs, timeout=_CLOSE_WAIT_SECONDS)
        if not alive:
            logger.info("kill_process: closed %s (%d processes)", name, len(gone))
            return f"Closed {name} ({len(gone)} process(es))."
        return (f"Asked {name} to close, but {len(alive)} of its {len(procs)} process(es) are still "
                "running -- it may be showing a 'save changes?' prompt. Tell the user; ending it "
                "by force (force=true) would lose unsaved work.")

    errors: list[str] = []
    for proc in procs:
        try:
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            errors.append(f"PID {proc.pid}: {e}")
    gone, alive = psutil.wait_procs(procs, timeout=3.0)
    msg = f"Force-ended {name} ({len(gone)} process(es))."
    if alive or errors:
        msg += f" Still running: {len(alive)}." + (f" Errors: {'; '.join(errors)}" if errors else "")
    logger.info("kill_process: %s", msg)
    return msg
