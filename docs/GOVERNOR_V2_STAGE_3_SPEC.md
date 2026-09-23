> **Historical document.** Written before the WinUI client, the current models and the
> 2026-09 clean-ups; parts of it no longer describe the system. For how Jarvis works today
> see `CLAUDE.md` and `PLAN.md` at the repo root.

# Governor V2 — Stage 3 Specification
## Manual Overrides, Error Recovery, and Desktop Command Panel

**Status:** Retroactive — written after implementation and review, to give Stage 4+
work a ground-truth document to diff against. Stage 3 shipped under engineering
judgment during live review rather than an upfront spec; this document reconstructs
acceptance criteria from the reviewed and approved implementation so the gap does
not repeat.

**Depends on:** `GOVERNOR_V2_STAGE_1_SPEC.md` (canonical `GovernorStatus` 6-tier enum,
hysteresis/debounce, `GovernorEvent` ring buffer), Stage 2 (`ProcessWatcher`,
`report_external_app`, `UnloadReason` per-app tracking).

**Referenced files:**
- `backend/app/governor/resource_governor.py`
- `backend/app/main.py`
- `backend/app/agent/permissions.py` (boundary reference only — see §6)
- `desktop/ui/index.html`, `desktop/ui/styles.css`, `desktop/ui/app.js`
- `backend/tests/test_governor.py`, `backend/tests/test_desktop.py`

---

## 1. Scope

Stage 3 gives a human operator direct, manual control over the governor's
automated behavior, and gives the desktop UI a way to surface and act on that
control. Three capability groups:

1. **Manual override core** — pause/resume automated governance, timed overrides
   that expire and auto-revert.
2. **Error recovery** — clear a stuck `ERROR` state, manually force a model
   reload, without bypassing the safety properties Stage 1/2 established.
3. **Desktop command panel** — a UI surface exposing (1) and (2), plus live
   status and a transition history timeline, without requiring the operator to
   hit the API directly.

---

## 2. Manual Override Core

### 2.1 Methods (`resource_governor.py`)

| Method | Behavior |
|---|---|
| `force_pause()` | Manually pauses automated governance. Logs a `trigger_reasons` entry. |
| `force_resume()` | Restores automated governance. Clears any active timed override. |
| `force_resume_ignore_metrics(duration_seconds: Optional[float])` | Forces healthy/IDLE reporting regardless of load, for `duration_seconds`. Passing `None` (or omitting) is an indefinite override. |

### 2.2 `is_manual_override` (property)
Returns `True` **only** when a timed override is active and not yet expired.
An indefinite override (`duration_seconds=None`) and an expired timed override
both return `False` here — this property answers "is there a live countdown,"
not "did a human intervene."

### 2.3 `override_expires_at` (property)
- Returns the unix timestamp the active timed override expires at.
- Returns `None` if no override is active, or if the active override is
  indefinite (`duration_seconds == float("inf")`).
- Must be computed from the same internal deadline the expiry-check loop uses
  (`_manual_resume_override_until`) — no separate/derived timer.

### 2.4 Live expiry & auto-reload
When a timed override's `duration_seconds` elapses:
- The poll loop detects expiry (`_is_resume_override_active` returns `False`
  once past deadline).
- `_trigger_reload_callback()` fires automatically to reload the model back
  into VRAM.
- Governor state reverts to automated governance — no manual step required.

---

## 3. Error Recovery

### 3.1 `clear_error() -> None`
- Clears `_error_state` and `_error_reason`.
- Logs a `trigger_reasons` entry: `"error cleared manually"`.
- Transitions status via `_check_status_transition()` — does **not** directly
  set `status = IDLE`; it re-evaluates through the normal transition path so
  hysteresis/history stay consistent.
- Does **not** by itself trigger a reload. A model that was unloaded when the
  error occurred remains unloaded after `clear_error()` — the operator (or
  `force_reload()`) must explicitly bring it back.

### 3.2 `force_reload() -> bool`
```python
def force_reload(self) -> bool:
    """
    Manually triggers model reload into GPU VRAM.
    Returns True if reload was initiated, False if model is already
    resident / no-op.
    """
```

**Guard (required):** must be a no-op — no callback fired, no state mutated —
when the model is already resident and healthy:
```
not self.model_unloaded and not self.pending_reload and not self._error_state
→ return False
```
This guard is load-bearing, not cosmetic. The desktop UI hides the "Force
Reload" button in this state as a convenience, but the backend must enforce
the same rule independently — the endpoint is reachable directly and must not
trust the UI to gate it.

**Settle-delay selection (required):** must not use a single hardcoded delay.
```python
settle_delay = 0.5 if bool(self._external_apps_active) else 0.1
```
- `0.5s` when any external app is currently tracked as active
  (`_external_apps_active` non-empty) — preserves the Bug 3 fix (avoid CUDA OOM
  from reloading before the WDDM/NVIDIA driver reclaims VRAM).
- `0.1s` only when no external app is active, i.e. genuinely idle-triggered
  manual recovery.

### 3.3 Retry isolation (required)
`_trigger_reload_callback()`'s 3-attempt exponential-backoff loop
(`attempt` counter) must be **local to each invocation** — a fresh closure/task
per call, never an instance attribute. Rationale: `clear_error()` followed by
`force_reload()` must get a full fresh 3-attempt cycle, not inherit exhausted
attempts from a prior failed reload. Exhaustion behavior (unchanged from
Stage 2/3 hardening): `_pending_reload = False`, `_error_state = True`,
status → `ERROR`, with `trigger_reasons=["model reload failed after maximum
retries"]`.

---

## 4. FastAPI Endpoints (`main.py`)

| Endpoint | Method | Body | Response |
|---|---|---|---|
| `/governor/pause` | POST | — | `{status, governor_status}` |
| `/governor/resume` | POST | — | `{status, governor_status}` |
| `/governor/resume-override` | POST | `{duration_seconds}` optional | `{status, governor_status, duration_seconds}` |
| `/governor/history` | GET | `?limit=N` | list of transition events, most recent first |
| `/governor/clear-error` | POST | — | `{status: "ok", governor_status}` |
| `/governor/force-reload` | POST | — | `{status: "ok"|"noop", governor_status, initiated: bool}` |
| `/governor/status` | GET | — | must include `override_expires_at` alongside existing fields |

`force-reload`'s response `status` field mirrors the return value of
`force_reload()`: `"noop"` + `initiated: false` when guarded off, `"ok"` +
`initiated: true` when a reload was actually triggered.

---

## 5. Desktop Command Panel

### 5.1 Governor pill (6-tier arc ring)
Replaces the single-dot status indicator. One visual state per
`GovernorStatus` tier:

| Tier | Color | Motion |
|---|---|---|
| IDLE | `#10b981` emerald | static glow |
| RUNNING | `#3b82f6` sapphire | static glow |
| LOADING | `#22d3ee` cyan | `arc-pulse` animation |
| PAUSED | `#ef4444` crimson | static glow |
| UNLOADED | `#f59e0b` amber | static glow |
| ERROR | `#ef4444` red | `arc-blink` animation |
| disconnected (no status response) | `#64748b` slate | none, no glow |

Pill label shows a live countdown (`Override: 4m 32s`, computed client-side
from `override_expires_at`, recomputed on each 2s poll — not a client-owned
`setInterval` ticking independently of server state) when `is_manual_override`
is true, otherwise `Governor: {status}`.

### 5.2 Command panel (click-to-open, click-outside-to-dismiss)
- **Pause / Resume** — single toggle button; label and icon flip based on
  current state (`PAUSED` or `manual_override_active` → shows "Resume").
- **Override 5m** — one-click `POST /governor/resume-override` with
  `duration_seconds: 300`.
- **Custom override** — numeric minutes input, converted to seconds
  (`Math.max(10, mins * 60)`), same endpoint.
- **Force Reload** — visible only when `model_unloaded || pending_reload`;
  calls `POST /governor/force-reload`. (Display-only gating — see §3.2 for the
  required backend-side guard this must not be relied on in place of.)
- **Clear Error** — visible only when status is `ERROR`; calls
  `POST /governor/clear-error`.
- **Transition history** — last 10 events from `GET /governor/history`,
  relative timestamps (`Ns ago` / `Nm ago`), `from_status → to_status`, and
  joined `raw_reasons`. Auto-refreshes on panel open and on each poll tick
  while the panel remains open.

### 5.3 Polling
Status poll interval: 2 seconds (`initTelemetry` → `setInterval(pollGovernor,
2000)`). Confirmed as an explicit product decision, not incidental.

---

## 6. Explicitly Out of Scope for Stage 3

- **Agent-tool exposure.** Governor controls (`force_reload`, `clear_error`,
  pause/resume) are direct, user-clicked HTTP endpoints — architecturally
  separate from the `evaluate_tool_permission` / `RiskTier`
  (LOW_RISK/CONFIRMATION_REQUIRED/HIGH_RISK) system that governs agent-invoked
  tool calls. **If any governor control is ever exposed as an agent-callable
  tool** (e.g. so Jarvis itself can decide to force-reload), it must be routed
  through `evaluate_tool_permission` at that point — this exemption applies
  only to direct UI-triggered calls and must not be assumed to carry over.
- ProcessWatcher internals, `governor_watchlist.json` merging/debouncing —
  Stage 2, already merged.
- RAG indexer / screen vision / scheduler / Telegram gateway — deferred to
  their respective OpenClaw phases.

---

## 7. Acceptance Checklist

| # | Requirement | Verifying test(s) |
|---|---|---|
| 1 | `override_expires_at` returns timestamp only while a timed (non-indefinite) override is active | `test_override_expires_at_and_manual_recovery_methods` |
| 2 | `clear_error()` clears error state and logs `"error cleared manually"` via `_check_status_transition` | `test_override_expires_at_and_manual_recovery_methods` |
| 3 | `force_reload()` no-ops (`False`, no callback) when model resident/healthy | `test_api_governor_status_and_overrides` (HTTP), `test_override_expires_at_and_manual_recovery_methods` (unit) |
| 4 | `force_reload()` initiates (`True`) when unloaded / pending / errored | same as #3 |
| 5 | Settle-delay is 0.5s when `_external_apps_active` non-empty, else 0.1s | manual code review (§3.2) — **no direct test found; recommend adding one** |
| 6 | Retry-attempt counter is local per invocation, not shared across calls | `test_retry_counter_local_isolation_across_invocations` |
| 7 | `POST /governor/clear-error` and `POST /governor/force-reload` return correct status/initiated payloads | `test_api_governor_status_and_overrides` |
| 8 | `GET /governor/status` includes `override_expires_at` | `test_api_governor_status_and_overrides` |
| 9 | 6-tier arc ring renders all tier classes + animations | `test_desktop_redesign_components` (presence checks only — no visual/behavioral test) |
| 10 | Command panel buttons wired to correct endpoints, contextual visibility (`Force Reload`, `Clear Error`) correct | `test_desktop_redesign_components` (presence checks only) |
| 11 | Transition history renders, auto-refreshes on open | `test_desktop_redesign_components` (presence check only) |

**Known test gaps to close in a follow-up pass, not blocking Stage 3 merge:**
- Item 5 (settle-delay selection) has no direct unit test — only confirmed by
  code inspection during review. Add a test asserting `_trigger_reload_callback`
  receives `0.5` when `_external_apps_active` is non-empty at `force_reload()`
  call time.
- Items 9–11 are HTML/JS string-presence assertions (`assert "govHistoryList"
  in html`), not behavioral tests. They confirm the markup exists, not that
  clicking buttons produces the right network calls or that the countdown
  renders correctly. Acceptable for Stage 3 given the manual review already
  performed, but Stage 4+ UI work should not rely on this level of test as
  sufficient on its own.

---

## 8. Review History

Reviewed across four rounds against Antigravity-submitted diffs (initial
submission → full unelided diffs + checklist → guard/settle-delay/retry
hardening → final verification). Key findings resolved during review, folded
into this spec as requirements rather than left as review comments:
`force_reload()` guard (§3.2), dynamic settle-delay (§3.2), retry-isolation
guarantee (§3.3), and the permission-boundary note (§6). 47/47 tests passing
at time of approval.
