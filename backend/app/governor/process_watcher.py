import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Union
import psutil

logger = logging.getLogger("jarvis.governor.watcher")


@dataclass
class WatchedProcess:
    match: str
    match_type: str = "exact"  # "exact" | "contains" (case-insensitive)
    label: str = ""
    game: bool = False  # a game: launching it through Jarvis turns Game mode on first (PLAN 4.16b)

    def matches(self, process_name: str) -> bool:
        if not process_name:
            return False
        p_name = process_name.lower().strip()
        m_target = self.match.lower().strip()
        if self.match_type == "contains":
            return m_target in p_name
        return p_name == m_target


DEFAULT_WATCHLIST = [
    WatchedProcess(match="UnrealEditor.exe", label="Unreal Engine"),
    WatchedProcess(match="UE4Editor.exe", label="Unreal Engine 4"),
    WatchedProcess(match="Adobe Premiere Pro.exe", label="Premiere Pro"),
    WatchedProcess(match="Resolve.exe", label="DaVinci Resolve"),
    WatchedProcess(match="blender.exe", label="Blender"),
    WatchedProcess(match="Unity.exe", label="Unity Editor"),
]


def load_watchlist(config_path: Optional[Union[str, Path]] = None) -> list[WatchedProcess]:
    """
    Load custom watched processes from JSON config and merge with DEFAULT_WATCHLIST.
    """
    watchlist = list(DEFAULT_WATCHLIST)
    if not config_path:
        # Default to governor_watchlist.json at project root
        config_path = Path(__file__).resolve().parent.parent.parent.parent / "governor_watchlist.json"
    
    path_obj = Path(config_path)
    if path_obj.exists():
        try:
            with open(path_obj, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and "match" in item:
                            match = item.get("match", "")
                            match_type = item.get("match_type", "exact")
                            label = item.get("label", match)
                            watchlist.append(
                                WatchedProcess(match=match, match_type=match_type, label=label,
                                               game=bool(item.get("game", True)))
                            )
            logger.info("Loaded %d watched process rules (including defaults) from %s", len(watchlist), path_obj)
        except Exception as e:
            logger.warning("Failed to load custom watchlist from %s: %s. Using defaults.", path_obj, e)
    else:
        logger.debug("No custom watchlist found at %s. Using default watchlist.", path_obj)

    return watchlist


class ProcessWatcher:
    """
    Background process watcher monitoring heavy 3D games, engines, and rendering tools.
    Provides deterministic fast-path throttle and unload signals before telemetry lags.
    """

    def __init__(
        self,
        watchlist: Optional[list[WatchedProcess]] = None,
        config_path: Optional[Union[str, Path]] = None,
        poll_interval: float = 2.0,
        launch_debounce_seconds: float = 4.0,
        recovery_debounce_seconds: float = 3.0,
    ):
        self.watchlist: list[WatchedProcess] = (
            watchlist if watchlist is not None else load_watchlist(config_path)
        )
        self.poll_interval = poll_interval
        self.launch_debounce_seconds = launch_debounce_seconds
        self.recovery_debounce_seconds = recovery_debounce_seconds

        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._governor: Any = None

        # Tracking state
        self._present_since: dict[str, float] = {}   # label -> timestamp first seen
        self._absent_since: dict[str, float] = {}    # label -> timestamp first missing
        self._confirmed_active: set[str] = set()    # set of confirmed running labels

    def scan(self) -> list[WatchedProcess]:
        """
        Scan running system processes once and return matched WatchedProcess items.
        """
        matched: list[WatchedProcess] = []
        seen_matches: set[str] = set()

        try:
            for p in psutil.process_iter(["name"]):
                try:
                    p_name = p.info.get("name")
                    if not p_name:
                        continue
                    for rule in self.watchlist:
                        if rule.label not in seen_matches and rule.matches(p_name):
                            matched.append(rule)
                            seen_matches.add(rule.label)
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
        except Exception as e:
            logger.debug("Error scanning processes: %s", e)

        return matched

    async def _poll_loop(self) -> None:
        logger.info(
            "ProcessWatcher background loop started (interval=%.1fs, launch_debounce=%.1fs, recovery_debounce=%.1fs)",
            self.poll_interval,
            self.launch_debounce_seconds,
            self.recovery_debounce_seconds
        )
        while self._running:
            try:
                now = time.time()
                current_matches = self.scan()
                matched_labels = {m.label for m in current_matches}

                # 1. Check for launches / ongoing presence
                for label in matched_labels:
                    self._absent_since.pop(label, None)
                    if label not in self._present_since:
                        self._present_since[label] = now

                    # Check if launch debounce threshold is met
                    time_present = now - self._present_since[label]
                    if label not in self._confirmed_active and time_present >= self.launch_debounce_seconds:
                        self._confirmed_active.add(label)
                        logger.info("External heavy application confirmed running: %s (after %.1fs)", label, time_present)
                        if self._governor:
                            self._governor.report_external_app(label, present=True)

                # 2. Check for closures / ongoing absence
                active_labels_copy = list(self._confirmed_active)
                for label in active_labels_copy:
                    if label not in matched_labels:
                        self._present_since.pop(label, None)
                        if label not in self._absent_since:
                            self._absent_since[label] = now

                        time_absent = now - self._absent_since[label]
                        if time_absent >= self.recovery_debounce_seconds:
                            self._confirmed_active.remove(label)
                            self._absent_since.pop(label, None)
                            logger.info("External heavy application confirmed closed: %s (after %.1fs)", label, time_absent)
                            if self._governor:
                                self._governor.report_external_app(label, present=False)

                # Also clear unconfirmed present_since entries if absent
                unconfirmed_labels = list(self._present_since.keys())
                for label in unconfirmed_labels:
                    if label not in matched_labels and label not in self._confirmed_active:
                        self._present_since.pop(label, None)

            except Exception as e:
                logger.error("Unexpected error in ProcessWatcher poll loop: %s", e)

            await asyncio.sleep(self.poll_interval)

    async def start(self, governor: Any) -> None:
        if self._running:
            return
        self._governor = governor
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._present_since.clear()
        self._absent_since.clear()
        self._confirmed_active.clear()
        logger.info("ProcessWatcher stopped.")
