> **Historical document.** Written before the WinUI client, the current models and the
> 2026-09 clean-ups; parts of it no longer describe the system. For how Jarvis works today
> see `CLAUDE.md` and `PLAN.md` at the repo root.

# Governor V2 — Stage 1 Implementation Spec: Activity Registry, Debounce, Observability

**Owner:** Pika
**Target file(s):** `governor.py` (core), model manager (wherever `llama-server` / Ollama load calls live)
**Supersedes:** the existing busy-state bugfix in `governor.py` (`set_inferencing` / `set_loading_model` / `is_busy`). This spec generalizes that fix — do not implement both independently.
**Parent spec:** `GOVERNOR_V2_SPEC.md` (§2.1, §2.2, §2.4, and task items 1–2 of §4)
**Sequencing:** Stage 1 of 3. Stage 2 (ProcessWatcher) depends on the `ActivityType` registry and `status` property built here — do not start Stage 2 before Stage 1 is merged and reviewed. Stage 3 (manual override endpoints) also builds on the `status` priority-order scaffold introduced here.
**Relationship to other in-flight work:** Independent of Stage A and Stage B — no shared surface area. Can proceed in parallel.

---

## 1. Problem statement

The current governor has two hardcoded booleans (`_is_inferencing`, `_is_loading_model`) to mean "Jarvis is busy, don't count this load against the throttle." That doesn't scale — future OpenClaw phases (RAG indexing, screen vision, scheduler jobs, Telegram-triggered actions) would each need their own bespoke boolean under the current design.

Separately, throttle detection is currently instantaneous: a single poll over threshold immediately flips `throttled = True`. This is too sensitive — a one-second CPU blip trips the same logic as sustained external load.

Finally, observability is log-only today. There's no queryable record of *why* the governor changed state at a given moment.

This stage fixes all three by replacing the booleans with an activity registry, adding hysteresis/debounce to throttle detection, and adding a bounded transition-history ring buffer.

---

## 2. Design

### 2.1 Activity registry (replaces the two booleans)

```python
class ActivityType(str, Enum):
    INFERENCING = "inferencing"
    MODEL_LOADING = "model_loading"
    RAG_INDEXING = "rag_indexing"
    SCREEN_VISION = "screen_vision"
    SCHEDULER_JOB = "scheduler_job"
    TELEGRAM_ACTION = "telegram_action"
```

Include all six values now even though only `INFERENCING` and `MODEL_LOADING` have real call sites today — the other four are placeholders for OpenClaw phases 2–8 and cost nothing to declare up front.

Governor holds a registry of *concurrently active* activities (a set of activity records, not a single flag — two things can legitimately overlap, e.g. a scheduler job that triggers inference):

```python
@dataclass
class ActivityRecord:
    activity_id: str
    activity_type: ActivityType
    label: Optional[str]
    started_at: float
```

New governor API:

```python
def begin_activity(self, activity_type: ActivityType, label: str | None = None) -> str:
    """Registers an active activity, returns an activity_id."""

def end_activity(self, activity_id: str) -> None:
    """Removes the activity. No-op if already ended."""

def activity(self, activity_type: ActivityType, label: str | None = None):
    """Async context manager wrapping begin_activity/end_activity."""

@property
def is_busy(self) -> bool:
    """True if any activity is currently registered."""

@property
def active_activities(self) -> list[ActivityRecord]:
    ...
```

Usage at any call site:

```python
async with governor.activity(ActivityType.MODEL_LOADING, label=model_name):
    await load_model()
```

**Backward compatibility:** keep `set_inferencing(bool)` and `set_loading_model(bool)` as thin wrappers that call `begin_activity` / `end_activity` under the hood with a fixed `activity_id` per type, so existing call sites don't break. Mark them `# deprecated, prefer governor.activity(...)` in a docstring.

### 2.2 Hysteresis / debounce

Replace instant threshold-breach = throttled with a two-sided debounce:

- `sustained_breach_polls` (default `3`): metric must breach threshold for this many **consecutive** polls before `metrics.throttled` flips to `True`.
- `recovery_polls` (default `2`): metric must be back under threshold for this many consecutive polls before `metrics.throttled` flips back to `False`.

Implementation: track a per-metric breach counter and a per-metric recovery counter on the governor instance itself (not in `SystemMetrics`, which stays a stateless snapshot). The *raw* instantaneous breach is still computed every poll (needed for the history log — see 2.3) but the *reported* `throttled` state only changes after crossing the debounce window.

- `SystemMetrics` gets a new field: `raw_throttled: bool` (instant, un-debounced) alongside the existing `throttled: bool` (which becomes the debounced value).
- Auto-unload's existing 4-poll streak requirement (`_throttle_streak >= 4`) now counts consecutive **debounced** throttled polls — effectively requiring `sustained_breach_polls + 4` raw breaching polls before firing. This is intentionally more conservative than today; that's expected, not a regression.

Also widen default thresholds slightly (real observed false-positives). Treat these as starting points Pika will tune post-deployment, not final values:

| Metric | Current default | New default |
|---|---|---|
| `gpu_threshold` | 85.0 | 88.0 |
| `vram_threshold` | 90.0 | 92.0 |
| `cpu_threshold` | 90.0 | 92.0 |
| `ram_threshold` | 92.0 | 94.0 |

Keep all four configurable via constructor args — don't hardcode.

### 2.3 Observability: status history ring buffer

Add a bounded in-memory ring buffer (`collections.deque(maxlen=50)`) recording every status *transition* (not every poll — only when the reported status enum changes):

```python
@dataclass
class GovernorEvent:
    timestamp: float
    from_status: str
    to_status: str
    raw_reasons: list[str]       # instantaneous breach reasons at time of transition
    metrics_snapshot: SystemMetrics
    active_activities: list[str] # activity_type values active at time of transition
```

New governor method:

```python
def get_history(self, limit: int = 50) -> list[GovernorEvent]:
    ...
```

No FastAPI endpoint yet — `GET /governor/history` is Stage 3's responsibility. This stage only needs the buffer and the in-process method to exist so Stage 3 can wire it up without touching `governor.py` internals again.

### 2.4 Status property (partial — Stage 1 scope only)

Add a `status` computed property now, but implement **only** the priority tiers this stage has the inputs for. Manual override (`manual_paused`, `manual_resume_override`) is Stage 3 scope and external-app detection (`external_app_active`) is Stage 2 scope — stub both as always-inactive for now so the property is correct today and only needs *additions*, not rewrites, in later stages:

```python
@property
def status(self) -> str:
    # Stage 3 will insert manual_paused and manual_resume_override checks above this line.
    # Stage 2 will insert external_app_active check above the is_busy check below.
    if self.is_busy:
        return "LOADING" if any(a.activity_type == ActivityType.MODEL_LOADING for a in self.active_activities) else "BUSY"
    if self.metrics.throttled:  # debounced value from §2.2
        return "THROTTLED"
    return "NORMAL"
```

This keeps the five-state enum (`NORMAL | LOADING | BUSY | THROTTLED | PAUSED`) stable across all three stages, even though `PAUSED` isn't reachable until Stage 3 lands.

---

## 3. File-by-file task breakdown

1. **`governor.py`**
   - Add `ActivityType` enum, `ActivityRecord`, `GovernorEvent` dataclasses.
   - Replace `_is_inferencing` / `_is_loading_model` booleans with `_active_activities: dict[str, ActivityRecord]`.
   - Add `begin_activity` / `end_activity` / `activity()` context manager.
   - Keep `set_inferencing` / `set_loading_model` as deprecated wrappers.
   - Add per-metric breach/recovery counters and debounce logic in `collect_metrics` (or a new wrapper around it, since `collect_metrics` is currently stateless per-call — the debounce state needs to live on `self`).
   - Add `raw_throttled` field to `SystemMetrics`.
   - Add `_history: deque[GovernorEvent]` and `get_history()`.
   - Add the partial `status` computed property per §2.4.
   - Update `_poll_loop` to compute status and append to `_history` on transition.
   - Update default thresholds per §2.2 table.

2. **Model manager** (wherever `llama-server` / Ollama load calls live)
   - Wrap load calls in `async with governor.activity(ActivityType.MODEL_LOADING, label=model_name):`.

---

## 4. Acceptance checklist

- [ ] Loading a model (Ollama or llama-server) never triggers `THROTTLED` or auto-unload, even under RTX 4060 8GB pressure.
- [ ] A single 1-second CPU/GPU spike does **not** flip status to `THROTTLED` — only sustained breaches across `sustained_breach_polls` do.
- [ ] `SystemMetrics.raw_throttled` reflects the instantaneous breach every poll, independent of the debounced `throttled` field.
- [ ] `set_inferencing(True)` (old API) still works unchanged for any call sites not yet migrated to `governor.activity(...)`.
- [ ] Two overlapping activities (e.g. a scheduler job that triggers inference, once that call site exists) don't cause premature "busy" clearing when one ends before the other — verify with two manually-nested `begin_activity` calls in a test even though no real scheduler exists yet.
- [ ] `get_history()` returns real transition events with reasons and a metrics snapshot, most recent first, capped at 50 entries.
- [ ] Auto-unload still fires for genuine sustained external load (no activity registered, no override) — confirm the `sustained_breach_polls + 4` combined streak requirement is what's actually enforced, not the old 4-poll count alone.
- [ ] `status` property returns exactly one of `NORMAL | LOADING | BUSY | THROTTLED` (never `PAUSED` — that's unreachable until Stage 3).
- [ ] New thresholds (88/92/92/94) are configurable via constructor args, not hardcoded.

---

## 5. Explicitly out of scope for this stage

- Manual override (`force_pause`, `force_resume`, `force_resume_ignore_metrics`) — Stage 3.
- `POST /governor/pause`, `/resume`, `/resume-override`, `GET /governor/history` FastAPI routes — Stage 3.
- ProcessWatcher, `governor_watchlist.json`, `report_external_app` — Stage 2.
- RAG indexer / screen vision / scheduler / Telegram gateway call-site wrapping — deferred until those OpenClaw phases actually exist; `ActivityType` values are declared now so no enum changes are needed later.
- Desktop UI pill — separate spec (`DESKTOP_UI_REDESIGN_SPEC.md`), waits on Stage 3.
