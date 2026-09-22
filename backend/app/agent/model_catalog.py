"""
Discovery and selection of the local GGUF models Jarvis can run.

Before this existed the two model slots were fixed by ``LLAMA_MAIN_MODEL_PATH`` /
``LLAMA_FAST_MODEL_PATH`` in ``.env`` and could not be changed without editing that file and
restarting the backend -- there was no endpoint, and ``/reliability/switch-backend`` is a
different, unrelated mechanism (it toggles reliability-monitor backends, not GGUFs). This module
adds the missing piece: it enumerates what is actually on disk, remembers a per-slot choice in
``data/models.json``, and is consulted by ``RuntimeProcessManager`` ahead of the settings values,
so a selection survives restarts without anyone editing configuration by hand.

Two discovery rules worth stating, because they encode a preference rather than a fact:

* **Files at most one directory deep under ``models/`` are recommended; anything deeper is
  not.** The top level and its immediate per-model folders (``models/qwen3.5-9b/``) are where
  deliberately-installed models live. Deeper trees tend to be vendor download mirrors
  (``models/unsloth/<repo>/...``) holding duplicates, variants and companion files, which are
  perfectly usable but are not what someone means by "my models".
* **``mmproj-*.gguf`` files are projectors, not chat models.** They are multimodal vision adapters
  that accompany a model and are loaded with ``--mmproj``; offering one as something to chat with
  would simply fail. They are reported separately, and a model is paired with the projector that
  sits in its own directory (``projector_for``), which is how ``RuntimeProcessManager`` decides
  whether to pass ``--mmproj`` -- the convention being one model per folder with its projector
  beside it.
"""

import json
import logging
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("jarvis.agent.model_catalog")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
DEFAULT_MODELS_DIR = REPO_ROOT / "models"
DEFAULT_STATE_PATH = REPO_ROOT / "data" / "models.json"

SLOTS = ("main", "fast")

# Substrings that identify a file as a multimodal projector rather than a chat model.
PROJECTOR_PREFIXES = ("mmproj",)


@dataclass
class ModelInfo:
    """One GGUF on disk, described the way a picker needs it."""

    id: str
    """Repo-relative POSIX path, e.g. ``models/Qwen3.5-9B-UD-Q3_K_XL.gguf``. Stable across
    machines and directly usable as ``LLAMA_MAIN_MODEL_PATH``."""

    name: str
    """File stem, for display."""

    size_bytes: int
    recommended: bool
    """True when the file sits in ``models/`` or in a folder directly under it, rather than deeper
    in a vendor download tree."""

    family: Optional[str]
    """Parameter-count hint parsed from the filename ("9B", "4B", ...), used to suggest which
    slot a model suits. None when the name says nothing about size."""

    directory: str
    """Repo-relative POSIX path of the containing directory, so a picker can group by source."""

    slots: list[str]
    """Which slots currently point at this file ("main", "fast", or both)."""

    projector: Optional[str] = None
    """Catalogue id of the multimodal projector paired with this model (the ``mmproj-*.gguf``
    sitting in the same directory), or None when the model is text-only."""


# Folders under models/ that hold sidecar models rather than chat models. The image captioner and the embedding sidecar
# lives in its own folder with its projector beside it, exactly like a chat model would, so
# without this it would be listed as a (tiny, useless) chat candidate.
SIDECAR_DIRS: frozenset[str] = frozenset({"captioner", "embeddings"})


def _is_sidecar(path: Path, models_dir: Path) -> bool:
    try:
        parts = path.resolve().relative_to(models_dir.resolve()).parts
    except ValueError:
        return False
    return bool(parts) and parts[0] in SIDECAR_DIRS


def _is_projector(path: Path) -> bool:
    lowered = path.name.lower()
    return any(lowered.startswith(prefix) for prefix in PROJECTOR_PREFIXES)


def _family_of(name: str) -> Optional[str]:
    """Parses a parameter-count token like '9B' or '4B' out of a filename."""
    for token in name.replace("_", "-").replace(".", "-").split("-"):
        stripped = token.strip().upper()
        if len(stripped) >= 2 and stripped.endswith("B") and stripped[:-1].isdigit():
            return stripped
    return None


class ModelCatalog:
    """
    Enumerates ``models/`` and remembers which file each slot should use.

    The selection is stored separately from ``.env`` deliberately: ``.env`` is hand-edited
    configuration under the user's control, and a UI that rewrote it would fight them for it.
    ``data/models.json`` holds only what the UI chose, and an absent entry means "fall back to
    whatever ``.env`` says", so deleting the file restores configured behaviour exactly.
    """

    def __init__(
        self,
        models_dir: Optional[Path] = None,
        state_path: Optional[Path] = None,
    ):
        self.models_dir = Path(models_dir) if models_dir else DEFAULT_MODELS_DIR
        self.state_path = Path(state_path) if state_path else DEFAULT_STATE_PATH
        self._selection: dict[str, str] = {}
        # What a launch configuration actually cost the card last time, in MiB, keyed by
        # ``launch_cost_key``. Measured by RuntimeProcessManager across a successful start and
        # consulted before the next one, so the VRAM budget check works from this machine's
        # numbers rather than a guess from file sizes.
        self._launch_costs: dict[str, float] = {}
        self._load_state()

    # --- persistence -----------------------------------------------------

    def _load_state(self) -> None:
        try:
            if self.state_path.exists():
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                selection = data.get("selection", {})
                self._selection = {
                    slot: str(value)
                    for slot, value in selection.items()
                    if slot in SLOTS and value
                }
                costs = data.get("launch_costs", {})
                self._launch_costs = {
                    str(key): float(value)
                    for key, value in costs.items()
                    if isinstance(value, (int, float)) and value > 0
                }
        except Exception as exc:
            logger.warning("Could not read model selection from %s: %s", self.state_path, exc)
            self._selection = {}
            self._launch_costs = {}

    def _save_state(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            state: dict[str, Any] = {"selection": self._selection}
            if self._launch_costs:
                state["launch_costs"] = self._launch_costs
            self.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning("Could not persist model selection to %s: %s", self.state_path, exc)

    # --- discovery -------------------------------------------------------

    def discover(self) -> list[ModelInfo]:
        """
        All selectable chat models, recommended (top-level) ones first, then alphabetical.

        Ordering is part of the contract: a client can present this list as-is and the first
        entries will be the ones worth recommending.
        """
        models: list[ModelInfo] = []
        if not self.models_dir.exists():
            return models

        for path in sorted(self.models_dir.rglob("*.gguf")):
            if _is_projector(path) or _is_sidecar(path, self.models_dir):
                continue
            models.append(self._describe(path))

        models.sort(key=lambda m: (not m.recommended, m.name.lower()))
        return models

    def projectors(self) -> list[ModelInfo]:
        """Multimodal projector files, reported separately from chat models."""
        if not self.models_dir.exists():
            return []
        return [
            self._describe(path)
            for path in sorted(self.models_dir.rglob("*.gguf"))
            if _is_projector(path) and not _is_sidecar(path, self.models_dir)
        ]

    def projector_for(self, model_path: Path) -> Optional[Path]:
        """
        The projector that belongs to a chat model: an ``mmproj-*.gguf`` in the same directory.

        A projector is only valid for the model it was converted from, and the per-folder layout
        is the one signal on disk that says which that is -- there is nothing in the filenames to
        match on (``mmproj-F16.gguf`` is what every Unsloth repo calls it). When a folder holds
        several (F16 next to F32, say) the smallest wins: they encode the same projector at
        different precisions and the smaller one costs less VRAM on an 8GB card.
        """
        if _is_projector(model_path):
            return None
        candidates = [
            p for p in model_path.parent.glob("*.gguf") if _is_projector(p) and p.is_file()
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda p: (p.stat().st_size, p.name))

    def _is_recommended(self, path: Path) -> bool:
        try:
            depth = len(path.resolve().relative_to(self.models_dir.resolve()).parts) - 1
        except ValueError:
            return False
        return depth <= 1

    def _describe(self, path: Path) -> ModelInfo:
        model_id = self._to_id(path)
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        projector = None if _is_projector(path) else self.projector_for(path)
        return ModelInfo(
            id=model_id,
            name=path.stem,
            size_bytes=size,
            recommended=self._is_recommended(path),
            family=_family_of(path.stem),
            directory=self._to_id(path.parent),
            slots=[slot for slot in SLOTS if self._selection.get(slot) == model_id],
            projector=self._to_id(projector) if projector else None,
        )

    def _to_id(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(REPO_ROOT).as_posix()
        except ValueError:
            return path.resolve().as_posix()

    # --- selection -------------------------------------------------------

    def selected(self, slot: str) -> Optional[str]:
        """The repo-relative path chosen for a slot, or None to defer to settings."""
        return self._selection.get(slot)

    def selection(self) -> dict[str, Optional[str]]:
        return {slot: self._selection.get(slot) for slot in SLOTS}

    def resolve_id(self, model_id: str) -> Path:
        """
        Turns a catalogue id back into an absolute path, rejecting anything that is not an
        existing ``.gguf`` inside ``models/``. The containment check is what stops a caller from
        pointing a slot at an arbitrary file elsewhere on the machine.
        """
        candidate = Path(model_id)
        if not candidate.is_absolute():
            candidate = REPO_ROOT / candidate
        candidate = candidate.resolve()

        models_root = self.models_dir.resolve()
        if not candidate.is_relative_to(models_root):
            raise ValueError(f"Model must live under {models_root}: {model_id}")
        if candidate.suffix.lower() != ".gguf" or not candidate.is_file():
            raise ValueError(f"Not an existing .gguf file: {model_id}")
        if _is_projector(candidate):
            raise ValueError(
                f"{candidate.name} is a multimodal projector, not a chat model; "
                "it is loaded alongside a model with --mmproj rather than selected as one."
            )
        return candidate

    def select(self, slot: str, model_id: str) -> ModelInfo:
        """Points a slot at a model and persists it. Raises ValueError on an invalid choice."""
        if slot not in SLOTS:
            raise ValueError(f"Unknown slot '{slot}'; expected one of {', '.join(SLOTS)}")

        resolved = self.resolve_id(model_id)
        self._selection[slot] = self._to_id(resolved)
        self._save_state()
        return self._describe(resolved)

    def clear(self, slot: str) -> None:
        """Drops a slot's override so it falls back to the configured .env path."""
        if self._selection.pop(slot, None) is not None:
            self._save_state()

    # --- learned launch costs --------------------------------------------

    @staticmethod
    def launch_cost_key(model_path: Path, projector: Optional[Path], ctx_size: int) -> str:
        """One launch configuration: which weights, which projector (or none), how much context."""
        return f"{model_path.name}|{projector.name if projector else 'text'}|{int(ctx_size)}"

    def launch_cost(self, model_path: Path, projector: Optional[Path], ctx_size: int) -> Optional[float]:
        """MiB this configuration cost the last time it started here, or None if never measured."""
        return self._launch_costs.get(self.launch_cost_key(model_path, projector, ctx_size))

    def record_launch_cost(self, model_path: Path, projector: Optional[Path], ctx_size: int, mib: float) -> None:
        if mib <= 0:
            return
        self._launch_costs[self.launch_cost_key(model_path, projector, ctx_size)] = round(float(mib), 1)
        self._save_state()


_catalog: Optional[ModelCatalog] = None


def get_model_catalog() -> ModelCatalog:
    global _catalog
    if _catalog is None:
        _catalog = ModelCatalog()
    return _catalog


def to_dict(info: ModelInfo) -> dict:
    return asdict(info)
