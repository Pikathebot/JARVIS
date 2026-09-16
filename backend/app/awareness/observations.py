"""
Observations: what Jarvis has noticed about the machine.

An Observation is a fact plus the two ways it needs to be presented — a short
title/detail for the UI, and a sentence written to be *spoken*. Rules produce
these; the monitor decides which ones are worth interrupting the user with.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class Severity(str, Enum):
    INFO = "info"
    NOTICE = "notice"
    WARNING = "warning"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return {"info": 0, "notice": 1, "warning": 2, "critical": 3}[self.value]


@dataclass
class Observation:
    """Something worth telling the user about, once."""

    kind: str
    severity: Severity
    title: str
    detail: str = ""
    # Written for text-to-speech: no units abbreviated, no markdown.
    spoken: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: f"obs_{uuid.uuid4().hex[:12]}")
    timestamp: float = field(default_factory=time.time)
    # Monotonic ordering assigned by the monitor. The wall clock is too coarse
    # on Windows to distinguish observations noticed in the same tick, so
    # clients page through history with this rather than the timestamp.
    seq: int = 0
    acknowledged: bool = False
    # True when this observation reports a condition returning to normal.
    resolved: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "seq": self.seq,
            "kind": self.kind,
            "severity": self.severity.value,
            "title": self.title,
            "detail": self.detail,
            "spoken": self.spoken or self.detail or self.title,
            "data": dict(self.data),
            "timestamp": self.timestamp,
            "acknowledged": self.acknowledged,
            "resolved": self.resolved,
        }


@dataclass
class SystemSnapshot:
    """One sampling of everything the rules can look at."""

    cpu_percent: float = 0.0
    ram_percent: float = 0.0
    ram_used_mb: float = 0.0
    ram_total_mb: float = 0.0
    gpu_available: bool = False
    gpu_name: Optional[str] = None
    gpu_util_percent: float = 0.0
    vram_used_mb: float = 0.0
    vram_total_mb: float = 0.0
    vram_free_mb: float = 0.0
    vram_util_percent: float = 0.0
    gpu_temp_c: Optional[float] = None
    disk_free_gb: float = 0.0
    disk_total_gb: float = 0.0
    disk_percent: float = 0.0
    battery_percent: Optional[float] = None
    battery_plugged: Optional[bool] = None
    governor_status: str = "IDLE"
    throttled: bool = False
    throttle_reasons: list[str] = field(default_factory=list)
    model_unloaded: bool = False
    heavy_apps: list[str] = field(default_factory=list)
    # Per heavy app, how long it has been running and what it is holding:
    # {"Blender": {"seconds": 640.0, "vram_mb": 2100.0, "ram_mb": 3400.0, "cpu_percent": 35.0}}
    heavy_app_details: dict[str, dict[str, float]] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cpu_percent": round(self.cpu_percent, 1),
            "ram_percent": round(self.ram_percent, 1),
            "ram_used_mb": round(self.ram_used_mb, 1),
            "ram_total_mb": round(self.ram_total_mb, 1),
            "gpu_available": self.gpu_available,
            "gpu_name": self.gpu_name,
            "gpu_util_percent": round(self.gpu_util_percent, 1),
            "vram_used_mb": round(self.vram_used_mb, 1),
            "vram_total_mb": round(self.vram_total_mb, 1),
            "vram_free_mb": round(self.vram_free_mb, 1),
            "vram_util_percent": round(self.vram_util_percent, 1),
            "gpu_temp_c": self.gpu_temp_c,
            "disk_free_gb": round(self.disk_free_gb, 1),
            "disk_total_gb": round(self.disk_total_gb, 1),
            "disk_percent": round(self.disk_percent, 1),
            "battery_percent": self.battery_percent,
            "battery_plugged": self.battery_plugged,
            "governor_status": self.governor_status,
            "throttled": self.throttled,
            "throttle_reasons": list(self.throttle_reasons),
            "model_unloaded": self.model_unloaded,
            "heavy_apps": list(self.heavy_apps),
            "heavy_app_details": {k: dict(v) for k, v in self.heavy_app_details.items()},
            "timestamp": self.timestamp,
        }
