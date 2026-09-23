> **Historical document.** Written before the WinUI client, the current models and the
> 2026-09 clean-ups; parts of it no longer describe the system. For how Jarvis works today
> see `CLAUDE.md` and `PLAN.md` at the repo root.

# Stage A Implementation Spec — Foundation Hardening

**For:** Antigravity (Gemini 3.7 Flash, thinking level: HIGH)
**Do not proceed to Stage B or any OpenClaw phase work. This spec covers Stage A only.**

## Before writing code

1. Read the existing `backend/app/agent/permissions.py`, `backend/app/agent/orchestrator.py`, and `backend/app/agent/tools/registry.py` in full — match their existing style, typing conventions, and error-handling patterns. Do not introduce a new framework or pattern where an existing one already covers the case.
2. **Propose the interface first.** Before writing implementation, output the exact function signatures, class definitions, and the point in `orchestrator.py` where each new component hooks in. Stop and wait for review before writing the bodies.
3. Do not modify `resource_governor.py`, `mcp/`, `voice/`, or `skills/` — out of scope for this stage.

---

## 1. `backend/app/agent/validator.py` (new file)

### 1.1 Data model
```python
class ValidationResult(BaseModel):
    valid: bool
    tool_name: str
    args: dict[str, Any]
    error: Optional[str] = None
    repair_attempt: int = 0  # 0 = first attempt, 1 = post-repair retry
```

### 1.2 Schema validation
```python
async def validate_tool_call(
    tool_name: str,
    raw_args: dict[str, Any],
    schema: type[BaseModel],
) -> ValidationResult:
```
- Attempt `schema.model_validate(raw_args)`.
- On `ValidationError`: return `ValidationResult(valid=False, error=<formatted pydantic error>)`.
- Pull `schema` from the existing tool registry (`registry.py`) — do not hardcode schemas here.

### 1.3 Repair-then-escalate flow (lives in orchestrator, not validator)
- On first `valid=False`: send the formatted error back to the model as a tool-result-style message, allow **one** re-attempt (`repair_attempt=1`).
- On second `valid=False` for the same logical call: do NOT retry against the same model tier again. Return a structured `EscalationRequired` signal to the model router so the request re-routes to Tier 3 (OpenRouter) for that turn's remaining tool calls.
- Log every attempt (both failures and the eventual success/escalation) via the audit logger (§3).

### 1.4 Duplicate/loop breaker
```python
class CallHistory:
    def __init__(self, window: int = 3): ...
    def record(self, tool_name: str, args: dict) -> None: ...
    def is_duplicate(self, tool_name: str, args: dict) -> bool:
        # True if the same (tool_name, canonicalized args) appeared
        # in the immediately preceding call within `window`
```
- Canonicalize args via `json.dumps(args, sort_keys=True)` before hashing — don't compare dicts directly (ordering).
- One `CallHistory` instance per conversation turn, not per session — reset at turn start.
- On `is_duplicate() == True` for a call repeated 2x consecutively: halt tool execution for that turn, surface a message to the user ("Jarvis attempted the same action twice — stopping to avoid a loop"), do not silently retry.

### 1.5 Per-turn cap
- Hard constant `MAX_TOOL_CALLS_PER_TURN = 15` in `config.py` (add alongside existing `governor_*` settings, same style).
- Orchestrator checks this before dispatching each tool call; on breach, halt and surface to user same as §1.4.

---

## 2. `backend/app/agent/permissions.py` (extend, don't rewrite)

- [ ] Add a config-driven `CONFIRMATION_TIMEOUT_ACTION: Literal["deny", "allow"] = "deny"` — locate wherever confirmation prompts currently resolve on timeout and make deny the hardcoded default, not configurable-to-allow (this should not be a footgun setting).
- [ ] Add per-tool rate limiting: `RATE_LIMITS: dict[str, int]` (tool_name → max calls per turn), checked independently of `MAX_TOOL_CALLS_PER_TURN`. Default unset = no per-tool limit beyond the global cap.
- [ ] Re-verify the canonical `act_<hash>` token check fires on **every** tool call in a multi-step chain — audit current code to confirm this isn't currently only checked on the first call of a turn. If it already re-checks every call, no change needed here — just confirm and note it in your response.
- [ ] Path canonicalization: locate existing workspace-boundary check (should be near `write_file`/`patch_file` permission classification). Confirm it resolves `..`, symlinks, and absolute-path escapes before comparing against the workspace root — add a test case for each if not already covered.

---

## 3. Audit logging (extend `backend/app/memory/store.py`, new table)

### Schema
```sql
CREATE TABLE IF NOT EXISTS tool_call_audit (
    call_id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL,
    timestamp REAL NOT NULL,
    tool_name TEXT NOT NULL,
    args_json TEXT NOT NULL,
    model_tier TEXT NOT NULL,          -- 'tier1' | 'tier2' | 'tier3'
    validation_result TEXT NOT NULL,   -- 'valid' | 'invalid_repaired' | 'invalid_escalated'
    permission_result TEXT NOT NULL,   -- 'allowed' | 'blocked' | 'confirmation_denied' | 'confirmation_timeout'
    executed BOOLEAN NOT NULL,
    error TEXT
);
```
- One row per tool call attempt, including malformed and blocked ones — this is the diagnostic surface for the "random Antigravity bugs" problem, so completeness matters more than minimalism here.
- Write via existing `store.py` connection patterns — don't open a separate SQLite connection.

---

## 4. Tests

Extend, don't replace, the existing files:

### `backend/tests/test_permissions.py` — add:
- `test_confirmation_timeout_denies_by_default`
- `test_rate_limit_per_tool_enforced`
- `test_action_token_reverified_on_each_call_in_chain`
- `test_path_traversal_blocked` (`../`, symlink escape, absolute path outside workspace — three separate cases)

### `backend/tests/test_tools.py` — add:
- `test_malformed_args_rejected_and_repairable`
- `test_second_malformed_attempt_escalates_tier`
- `test_duplicate_call_triggers_loop_breaker`
- `test_turn_tool_call_cap_enforced`

### New: `backend/tests/test_validator.py`
- Direct unit tests for `validate_tool_call`, `CallHistory.is_duplicate`, canonicalization edge cases (arg key ordering, nested dicts).

All new tests must pass against the **existing** two tools (`web_search`, `fetch_url`) — no new tools are being added in this stage.

---

## 5. Explicitly out of scope for this stage

Do not touch: Bonsai/model_router changes, GBNF grammars, any Phase 2+ tools (`write_file`, `patch_file`, OS controls, etc.), Telegram, scheduler. If you find yourself wanting to modify any of those to make Stage A "work better," stop and flag it instead of proceeding.
