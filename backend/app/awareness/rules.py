"""
Awareness rules.

Each rule looks at one snapshot and decides whether the machine is in a state
worth mentioning. Rules are pure functions of the snapshot: no state, no
timers, no de-duplication — the monitor owns all of that, so a rule can be
read and tested on its own.

A rule returns None when the condition is clear. That "clear" answer is
meaningful: the monitor uses it to announce recovery for a condition it
previously warned about.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from app.awareness.observations import Observation, Severity, SystemSnapshot


@dataclass(frozen=True)
class Thresholds:
    vram_warning_percent: float = 88.0
    # Above the main model's own steady state: the 9B + projector + KV sits at ~97% of the
    # 4060's 8 GB with an ordinary desktop behind it. At 96 this fired every session and the
    # registered action evicted the very model that accounted for the usage -- mid-turn.
    vram_critical_percent: float = 98.5
    gpu_temp_warning_c: float = 80.0
    gpu_temp_critical_c: float = 87.0
    ram_warning_percent: float = 88.0
    cpu_warning_percent: float = 92.0
    disk_warning_gb: float = 20.0
    disk_critical_gb: float = 5.0
    battery_warning_percent: float = 20.0
    battery_critical_percent: float = 10.0
    # A heavy external app that stays open this long gets a nudge with numbers
    # rather than the initial "I am yielding the GPU" notice.
    heavy_app_nudge_seconds: float = 600.0


def _gb(mb: float) -> float:
    return round(mb / 1024.0, 1)


def vram_pressure(snapshot: SystemSnapshot, t: Thresholds) -> Optional[Observation]:
    if not snapshot.gpu_available or snapshot.vram_total_mb <= 0:
        return None

    used_gb, total_gb = _gb(snapshot.vram_used_mb), _gb(snapshot.vram_total_mb)
    percent = snapshot.vram_util_percent

    if percent >= t.vram_critical_percent:
        return Observation(
            kind="vram_pressure",
            severity=Severity.CRITICAL,
            title=f"VRAM critical — {used_gb} / {total_gb} GB",
            detail=f"GPU memory is at {percent:.0f}%. Model eviction is imminent.",
            spoken=(
                f"VRAM is at {used_gb} of {total_gb} gigabytes{{address}}. "
                "I am about to have to evict the model."
            ),
            data={"vram_used_mb": snapshot.vram_used_mb, "percent": percent},
        )

    if percent >= t.vram_warning_percent:
        return Observation(
            kind="vram_pressure",
            severity=Severity.WARNING,
            title=f"VRAM high — {used_gb} / {total_gb} GB",
            detail=f"GPU memory is at {percent:.0f}%.",
            spoken=f"GPU memory is at {percent:.0f} percent, {used_gb} of {total_gb} gigabytes.",
            data={"vram_used_mb": snapshot.vram_used_mb, "percent": percent},
        )

    return None


def gpu_thermal(snapshot: SystemSnapshot, t: Thresholds) -> Optional[Observation]:
    temp = snapshot.gpu_temp_c
    if temp is None:
        return None

    if temp >= t.gpu_temp_critical_c:
        return Observation(
            kind="gpu_thermal",
            severity=Severity.CRITICAL,
            title=f"GPU at {temp:.0f}°C",
            detail="The GPU is thermally throttling. Sustained load should be reduced.",
            spoken=(
                f"The GPU is at {temp:.0f} degrees and throttling{{address}}. "
                "I would ease off the load."
            ),
            data={"gpu_temp_c": temp},
        )

    if temp >= t.gpu_temp_warning_c:
        return Observation(
            kind="gpu_thermal",
            severity=Severity.WARNING,
            title=f"GPU at {temp:.0f}°C",
            detail="GPU temperature is climbing under sustained load.",
            spoken=f"The GPU is running warm, {temp:.0f} degrees.",
            data={"gpu_temp_c": temp},
        )

    return None


def ram_pressure(snapshot: SystemSnapshot, t: Thresholds) -> Optional[Observation]:
    if snapshot.ram_percent < t.ram_warning_percent or snapshot.ram_total_mb <= 0:
        return None

    used_gb, total_gb = _gb(snapshot.ram_used_mb), _gb(snapshot.ram_total_mb)
    return Observation(
        kind="ram_pressure",
        severity=Severity.WARNING,
        title=f"System RAM at {snapshot.ram_percent:.0f}%",
        detail=f"{used_gb} of {total_gb} GB in use.",
        spoken=f"System memory is at {snapshot.ram_percent:.0f} percent, {used_gb} of {total_gb} gigabytes.",
        data={"ram_percent": snapshot.ram_percent},
    )


def cpu_saturation(snapshot: SystemSnapshot, t: Thresholds) -> Optional[Observation]:
    if snapshot.cpu_percent < t.cpu_warning_percent:
        return None

    return Observation(
        kind="cpu_saturation",
        severity=Severity.NOTICE,
        title=f"CPU at {snapshot.cpu_percent:.0f}%",
        detail="Sustained CPU saturation; responses may be slower than usual.",
        spoken=f"The CPU is pinned at {snapshot.cpu_percent:.0f} percent.",
        data={"cpu_percent": snapshot.cpu_percent},
    )


def disk_space(snapshot: SystemSnapshot, t: Thresholds) -> Optional[Observation]:
    if snapshot.disk_total_gb <= 0:
        return None

    free = snapshot.disk_free_gb
    if free <= t.disk_critical_gb:
        return Observation(
            kind="disk_space",
            severity=Severity.CRITICAL,
            title=f"Only {free:.1f} GB free",
            detail="Disk space is nearly exhausted; model and index writes will fail.",
            spoken=f"Disk space is down to {free:.1f} gigabytes{{address}}. Writes will start failing.",
            data={"disk_free_gb": free},
        )

    if free <= t.disk_warning_gb:
        return Observation(
            kind="disk_space",
            severity=Severity.WARNING,
            title=f"{free:.1f} GB free",
            detail="Disk space is getting low.",
            spoken=f"Disk space is down to {free:.0f} gigabytes.",
            data={"disk_free_gb": free},
        )

    return None


def battery_level(snapshot: SystemSnapshot, t: Thresholds) -> Optional[Observation]:
    percent = snapshot.battery_percent
    if percent is None or snapshot.battery_plugged:
        return None

    if percent <= t.battery_critical_percent:
        return Observation(
            kind="battery_level",
            severity=Severity.CRITICAL,
            title=f"Battery at {percent:.0f}%",
            detail="On battery and nearly empty. GPU inference will drain it quickly.",
            spoken=f"Battery is at {percent:.0f} percent{{address}}. I would find a charger.",
            data={"battery_percent": percent},
        )

    if percent <= t.battery_warning_percent:
        return Observation(
            kind="battery_level",
            severity=Severity.NOTICE,
            title=f"Battery at {percent:.0f}%",
            detail="Running on battery.",
            spoken=f"You are on battery, {percent:.0f} percent remaining.",
            data={"battery_percent": percent},
        )

    return None


def model_evicted(snapshot: SystemSnapshot, t: Thresholds) -> Optional[Observation]:
    if not snapshot.model_unloaded:
        return None

    return Observation(
        kind="model_evicted",
        severity=Severity.NOTICE,
        title="Model evicted from VRAM",
        detail="The governor unloaded the model to free GPU memory. It reloads on the next request.",
        spoken="I have unloaded the model to free up GPU memory. It will reload on your next request.",
        data={"model_unloaded": True},
    )


def _minutes(seconds: float) -> str:
    minutes = max(1, int(round(seconds / 60.0)))
    return "a minute" if minutes == 1 else f"{minutes} minutes"


def _heavy_app_nudge(snapshot: SystemSnapshot, label: str, details: dict[str, float]) -> str:
    """
    One sentence with the numbers that matter: what the app is holding, for how long, and
    what that leaves Jarvis. Only the figures that could actually be read are mentioned.
    """
    held = []
    vram = details.get("vram_mb", 0.0)
    ram = details.get("ram_mb", 0.0)
    cpu = details.get("cpu_percent", 0.0)
    if vram >= 256:
        held.append(f"{_gb(vram)} gigabytes of VRAM")
    if ram >= 1024:
        held.append(f"{_gb(ram)} gigabytes of RAM")
    if cpu >= 25:
        held.append(f"{cpu:.0f} percent of the CPU")
    for_how_long = _minutes(details.get("seconds", 0.0))
    if held:
        first = f"{label} has held {' and '.join(held)} for {for_how_long}"
    else:
        first = f"{label} has been running for {for_how_long}"

    if snapshot.gpu_available and snapshot.vram_total_mb > 0:
        free_gb = _gb(snapshot.vram_free_mb)
        if snapshot.model_unloaded:
            effect = f"the GPU has {free_gb} gigabytes free and my model stays unloaded until it closes"
        elif free_gb < 1.5:
            effect = f"the GPU has only {free_gb} gigabytes free, so my model has little headroom"
        else:
            effect = f"the GPU has {free_gb} gigabytes free"
    else:
        effect = "I am yielding the GPU while it runs"
    return f"{first}; {effect}. Closing it would give that back{{address}}."


def heavy_external_app(snapshot: SystemSnapshot, t: Thresholds) -> Optional[Observation]:
    if not snapshot.heavy_apps:
        return None

    apps = ", ".join(snapshot.heavy_apps)
    data = {"heavy_apps": list(snapshot.heavy_apps), "details": snapshot.heavy_app_details}

    # Sustained: the app that has been open longest gets the nudge. Escalating to WARNING
    # is what makes the monitor announce (and speak) it after the initial notice.
    sustained = [
        (label, details)
        for label, details in snapshot.heavy_app_details.items()
        if label in snapshot.heavy_apps and details.get("seconds", 0.0) >= t.heavy_app_nudge_seconds
    ]
    if sustained:
        label, details = max(sustained, key=lambda item: item[1].get("seconds", 0.0))
        nudge = _heavy_app_nudge(snapshot, label, details)
        return Observation(
            kind="heavy_external_app",
            severity=Severity.WARNING,
            title=f"{label} has been running for {_minutes(details.get('seconds', 0.0))}",
            detail=nudge.replace("{address}", ""),
            spoken=nudge,
            data=data,
        )

    return Observation(
        kind="heavy_external_app",
        severity=Severity.NOTICE,
        title=f"Heavy application running: {apps}",
        detail="Jarvis is yielding GPU resources while it runs.",
        spoken=f"{apps} is running, so I am yielding the GPU.",
        data=data,
    )


RuleFn = Callable[[SystemSnapshot, Thresholds], Optional[Observation]]

DEFAULT_RULES: tuple[RuleFn, ...] = (
    vram_pressure,
    gpu_thermal,
    ram_pressure,
    cpu_saturation,
    disk_space,
    battery_level,
    model_evicted,
    heavy_external_app,
)

RECOVERY_TEXT: dict[str, str] = {
    "vram_pressure": "GPU memory is back to normal.",
    "gpu_thermal": "The GPU has cooled down.",
    "ram_pressure": "System memory has recovered.",
    "cpu_saturation": "CPU load has settled.",
    "disk_space": "Disk space has recovered.",
    "battery_level": "Back on mains power.",
    "model_evicted": "The model is loaded again.",
    "heavy_external_app": "The heavy application has closed; full resources are available again.",
}
