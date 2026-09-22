import logging
import time

logger = logging.getLogger("jarvis.agent.tools.system_status")


def get_system_status() -> str:
    """
    Measure the machine right now: GPU load, VRAM used/total and temperature, RAM, CPU, free disk,
    battery, which local model is loaded, and any heavy external apps. Call this whenever the
    user asks about system usage, resources, performance, temperatures, or what is loaded — the
    figures it returns are the only hardware numbers you may quote.
    """
    try:
        from app.awareness.briefing import system_state_line
        from app.main import awareness_monitor
    except Exception as e:  # pragma: no cover - only outside the running app
        return f"Error: system telemetry is unavailable ({e})."

    try:
        # A fresh sample rather than the monitor's last poll: the poll runs every ~20 s and a
        # deliberate "how hot is the GPU" deserves the number as of now.
        snapshot = awareness_monitor.collect_snapshot()
    except Exception as e:
        logger.warning("Fresh hardware sample failed (%s); using the last poll", e)
        snapshot = awareness_monitor.last_snapshot
        if snapshot is None:
            return "Error: no hardware sample is available."

    loaded = None
    try:
        from app.agent.runtime_process_manager import get_runtime_process_manager

        manager = get_runtime_process_manager()
        if manager.is_running() and manager.current_model_kind:
            resolved, _ = manager.resolve_model_path(manager.current_model_kind)
            loaded = f"{resolved.stem} ({manager.current_model_kind} slot)"
    except Exception:
        pass

    line = system_state_line(snapshot, loaded_model=loaded)
    extras = []
    if snapshot.governor_status:
        extras.append(f"governor: {snapshot.governor_status}")
    if snapshot.throttled and snapshot.throttle_reasons:
        extras.append("throttled because: " + ", ".join(snapshot.throttle_reasons))
    age = max(0.0, time.time() - snapshot.timestamp)
    measured = "measured just now" if age < 5 else f"measured {age:.0f} s ago"
    return f"System status ({measured}): {line}" + (("; " + "; ".join(extras)) if extras else "")
