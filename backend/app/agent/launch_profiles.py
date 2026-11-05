"""
Launch profiles (PLAN 4.28 items 2-3): which model and context each slot starts with, chosen from the card's free
VRAM at launch, kept in data rather than spread through the launcher as thresholds.

The table ships with one profile, "as configured", that sets nothing: every slot launches exactly as ``.env`` and
the model picker say, as before this file existed. Another machine adds buckets in ``data/launch_profiles.json``
(git-ignored, hand-edited for now; a Settings page and a first-run probe are 4.28's open questions):

    {"default": "8 GB card",
     "profiles": [
       {"name": "small card", "min_free_mb": 0,    "main_model": "models/Qwen3.5-4B/Qwen3.5-4B-Q4_K_M.gguf",
        "main_ctx": 8192, "projector": false},
       {"name": "8 GB card",  "min_free_mb": 6000},
       {"name": "big card",   "min_free_mb": 12000, "main_ctx": 49152}
     ]}

The profile with the highest ``min_free_mb`` that the card's free VRAM reaches wins; when NVML can't say, the
``default`` one (else the first listed). The bucket edges above are placeholders: 4.28 says to measure them on the
4060 with the current 9B before trusting any. A field a profile leaves out keeps today's source:

* model: a model picked in the UI (``data/models.json``) > the profile's ``main_model`` / ``fast_model`` > ``.env``;
* context: a ``ctx_size`` given to the manager in code > the profile's ``main_ctx`` / ``fast_ctx`` > ``.env``;
* projector: the profile's ``projector: false`` turns it off for that bucket; otherwise ``LLAMA_MMPROJ_ENABLED`` and
  the catalogue's pairing decide, as before.

The launch ladder (no projector, half context, the fast model) still runs inside whichever bucket is chosen: the
buckets only choose where it starts. A file that doesn't parse or validate is logged and ignored -- it never stops a
launch -- and ``table_error`` says why on ``/api/models``.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("jarvis.agent.launch_profiles")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
TABLE_PATH = REPO_ROOT / "data" / "launch_profiles.json"
MIN_CTX = 2048
MAX_CTX = 1_048_576
_SLOTS = ("main", "fast")


@dataclass(frozen=True)
class Profile:
    name: str
    min_free_mb: float = 0.0
    main_model: Optional[str] = None
    fast_model: Optional[str] = None
    main_ctx: Optional[int] = None
    fast_ctx: Optional[int] = None
    projector: Optional[bool] = None

    def model_for(self, slot: str) -> Optional[str]:
        return self.main_model if slot == "main" else self.fast_model if slot == "fast" else None

    def ctx_for(self, slot: str) -> Optional[int]:
        return self.main_ctx if slot == "main" else self.fast_ctx if slot == "fast" else None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


AS_CONFIGURED = Profile(name="as configured", min_free_mb=0.0)


@dataclass(frozen=True)
class Table:
    profiles: tuple[Profile, ...]
    default: Profile
    source: str                  # "built-in" or the file's path
    error: Optional[str] = None  # why the file was ignored, if it was

    def pick(self, free_mb: Optional[float]) -> Profile:
        if free_mb is None:
            return self.default
        reached = [p for p in self.profiles if p.min_free_mb <= free_mb]
        if not reached:  # below every edge: the smallest bucket, the likeliest to fit
            return min(self.profiles, key=lambda p: p.min_free_mb)
        return max(reached, key=lambda p: p.min_free_mb)


BUILT_IN = Table(profiles=(AS_CONFIGURED,), default=AS_CONFIGURED, source="built-in")


class TableError(ValueError):
    pass


def _profile(raw: Any, n: int) -> Profile:
    if not isinstance(raw, dict):
        raise TableError(f"profile {n} is not an object")
    known = {"name", "min_free_mb", "projector"} | {f"{s}_{f}" for s in _SLOTS for f in ("model", "ctx")}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise TableError(f"profile {n} has unknown field(s) {', '.join(unknown)} (known: {', '.join(sorted(known))})")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise TableError(f"profile {n} needs a name")
    edge = raw.get("min_free_mb", 0)
    if isinstance(edge, bool) or not isinstance(edge, (int, float)) or edge < 0:
        raise TableError(f"profile '{name}': min_free_mb must be a number of MiB, 0 or more")
    fields: dict[str, Any] = {}
    for slot in _SLOTS:
        model = raw.get(f"{slot}_model")
        if model is not None:
            if not isinstance(model, str) or not model.strip():
                raise TableError(f"profile '{name}': {slot}_model must be a path to a .gguf file")
            fields[f"{slot}_model"] = model.strip()
        ctx = raw.get(f"{slot}_ctx")
        if ctx is not None:
            if isinstance(ctx, bool) or not isinstance(ctx, int) or not MIN_CTX <= ctx <= MAX_CTX:
                raise TableError(f"profile '{name}': {slot}_ctx must be a whole number of tokens, {MIN_CTX}-{MAX_CTX}")
            fields[f"{slot}_ctx"] = ctx
    projector = raw.get("projector")
    if projector is not None and not isinstance(projector, bool):
        raise TableError(f"profile '{name}': projector must be true or false")
    return Profile(name=name.strip(), min_free_mb=float(edge), projector=projector, **fields)


def parse_table(data: Any, source: str = "data") -> Table:
    if not isinstance(data, dict) or not isinstance(data.get("profiles"), list) or not data["profiles"]:
        raise TableError('the file must be {"profiles": [ ... at least one profile ... ]}')
    profiles = tuple(_profile(p, n) for n, p in enumerate(data["profiles"], 1))
    names = [p.name for p in profiles]
    if len(set(names)) != len(names):
        raise TableError("two profiles have the same name")
    edges = [p.min_free_mb for p in profiles]
    if len(set(edges)) != len(edges):
        raise TableError("two profiles have the same min_free_mb, so which one wins would be a coin toss")
    wanted = data.get("default")
    if wanted is None:
        default = profiles[0]
    else:
        match = [p for p in profiles if p.name == wanted]
        if not match:
            raise TableError(f"default '{wanted}' is not one of the profiles ({', '.join(names)})")
        default = match[0]
    return Table(profiles=profiles, default=default, source=source)


def load_table(path: Optional[Path] = None) -> Table:
    """The table from ``data/launch_profiles.json``, or the built-in one when there is no file or it is bad."""
    path = Path(path) if path is not None else TABLE_PATH
    if not path.exists():
        return BUILT_IN
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return parse_table(data, source=str(path))
    except (OSError, ValueError) as exc:  # TableError and json.JSONDecodeError are ValueErrors
        message = f"{path.name} was ignored: {exc}"
        logger.warning("Launch profiles: %s. Using the built-in profile.", message)
        return Table(profiles=BUILT_IN.profiles, default=BUILT_IN.default, source="built-in", error=message)
