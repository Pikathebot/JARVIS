"""
Runtime persona selection and persistence.

The active persona survives restarts via a small JSON file next to the
database, so a user who switched to 'operator' for hands-free use does not get
the verbose persona back on the next launch.
"""
from __future__ import annotations

import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any, Optional

from app.atomic_write import write_text_atomic
from app.config import BASE_DIR, settings
from app.persona.profiles import (
    BUILTIN_PERSONAS,
    DEFAULT_PERSONA_ID,
    PersonaProfile,
)

logger = logging.getLogger("jarvis.persona")

DEFAULT_STATE_PATH = BASE_DIR.parent / "data" / "persona.json"


class PersonaManager:
    """Holds the active persona and applies user overrides on top of it."""

    def __init__(self, state_path: Optional[Path] = None):
        self.state_path = Path(state_path) if state_path else DEFAULT_STATE_PATH
        self._active_id: str = DEFAULT_PERSONA_ID
        self._overrides: dict[str, Any] = {}
        self._load_state()

    # ---------------------------------------------------------------- state

    def _load_state(self) -> None:
        configured = (getattr(settings, "persona_id", "") or "").strip().lower()
        if configured in BUILTIN_PERSONAS:
            self._active_id = configured

        try:
            if self.state_path.exists():
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                saved_id = str(data.get("active_id", "")).lower()
                if saved_id in BUILTIN_PERSONAS:
                    self._active_id = saved_id
                overrides = data.get("overrides")
                if isinstance(overrides, dict):
                    self._overrides = self._clean_overrides(overrides)
        except Exception as e:
            logger.warning("Could not read persona state from %s: %s", self.state_path, e)

    def _save_state(self) -> None:
        try:
            write_text_atomic(
                self.state_path,
                json.dumps({"active_id": self._active_id, "overrides": self._overrides}, indent=2),
            )
        except Exception as e:
            logger.warning("Could not persist persona state to %s: %s", self.state_path, e)

    @staticmethod
    def _clean_overrides(raw: dict[str, Any]) -> dict[str, Any]:
        """Only allow overriding presentation fields, never the persona's identity."""
        allowed = {"address_term", "voice_id", "max_speech_sentences", "greeting"}
        cleaned: dict[str, Any] = {}
        for key, value in raw.items():
            if key not in allowed or value is None:
                continue
            if key == "max_speech_sentences":
                try:
                    cleaned[key] = max(0, int(value))
                except (TypeError, ValueError):
                    continue
            else:
                cleaned[key] = str(value)
        return cleaned

    # --------------------------------------------------------------- access

    @property
    def active_id(self) -> str:
        return self._active_id

    def list_personas(self) -> list[dict[str, Any]]:
        return [p.to_dict() for p in BUILTIN_PERSONAS.values()]

    def get_active(self) -> PersonaProfile:
        """The active persona with user overrides applied."""
        base = BUILTIN_PERSONAS.get(self._active_id, BUILTIN_PERSONAS[DEFAULT_PERSONA_ID])
        if not self._overrides:
            return base
        return replace(base, **self._overrides)

    def set_active(self, persona_id: str) -> PersonaProfile:
        key = (persona_id or "").strip().lower()
        if key not in BUILTIN_PERSONAS:
            raise ValueError(
                f"Unknown persona '{persona_id}'. Available: {', '.join(BUILTIN_PERSONAS)}"
            )
        self._active_id = key
        self._save_state()
        logger.info("Active persona set to '%s'", key)
        return self.get_active()

    def set_overrides(self, overrides: dict[str, Any]) -> PersonaProfile:
        self._overrides = self._clean_overrides(overrides or {})
        self._save_state()
        return self.get_active()

    def clear_overrides(self) -> PersonaProfile:
        self._overrides = {}
        self._save_state()
        return self.get_active()

    # ------------------------------------------------------------- shaping

    def build_prompt_preamble(self) -> str:
        return self.get_active().build_prompt_preamble()

    def shape_for_speech(self, text: str) -> str:
        return self.get_active().shape_for_speech(text)

    def status(self) -> dict[str, Any]:
        active = self.get_active()
        return {
            "active_id": self._active_id,
            "active": active.to_dict(),
            "overrides": dict(self._overrides),
            "available": [
                {"id": p.id, "name": p.name, "description": p.description}
                for p in BUILTIN_PERSONAS.values()
            ],
        }


persona_manager = PersonaManager()
