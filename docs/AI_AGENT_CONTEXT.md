# Jarvis — Onboarding for AI Agents

> Current as of 2026-10-25. This is a short map for an agent starting cold. The working rules, the commands and
> the contracts that are easy to break are in `CLAUDE.md`, which wins where anything disagrees. `PLAN.md` says what
> is built and what is next; `docs/MILESTONES.md` says why it was built that way.

## 1. What this is

A private assistant that lives on one Windows 11 laptop with an 8 GB RTX 4060. It answers and acts (files, apps,
commands, web, Google Calendar and Tasks), speaks and listens locally, and keeps its own model inside the VRAM the
desktop leaves free. The user's stated goal: it can do anything, works with the user on whatever the work is, and
steps aside when the laptop is needed for heavy use.

Principles that have held up:
- Local first. Cloud paths are explicit opt-ins only.
- Measure, then change. Quants, KV types, VRAM budgets and glass constants were settled by measurement.
- The backend decides meaning; the client renders. The seam is HTTP and SSE.
- One channel for anything unprompted: `AwarenessMonitor.emit`.

## 2. Where to look

| Question | Start here |
|---|---|
| How a chat turn flows | `backend/app/agent/orchestrator.py` (`run_stream`), then `llamacpp_provider.py` |
| Which tools exist and how they are offered | `backend/app/agent/tools/registry.py` (`TOOL_FUNCTIONS`, `TOOL_SCHEMAS`, `tools_for_space()`) |
| Whether an action asks first | `backend/app/agent/permissions.py` (`BASE_TOOL_RISK_MAP`) |
| The approval token and its expiry | `backend/app/agent/confirmations.py` |
| Which model serves which slot | `data/models.json`, then `GET /api/models` |
| VRAM, eviction and the launch ladder | `backend/app/governor/`, `agent/runtime_process_manager.py` |
| Proactive remarks | `backend/app/awareness/monitor.py` |
| Freeform thread and episodes | `backend/app/thread_live.py`, `routers/thread.py` |
| Voice | `backend/app/voice/` (`session.py` for the wake word and follow-up window) |
| Skills | `skills/*.md` and `backend/app/skills/loader.py` |
| The client | `desktop-winui/` (`Jarvis.App`, `Jarvis.Core`, `Jarvis.Glass`) |

## 3. Rules that bite

- **One tool system.** `backend/app/agent/tools/`. The older `BaseTool` stack, the Ollama and LM Studio providers,
  and the backend-side Chatterbox speech were deleted on 2026-09-23. Docs or `.env` keys that mention them are stale.
- **Adding a tool needs four things**: the function in `TOOL_FUNCTIONS` and `AVAILABLE_TOOLS`, a Pydantic `*Args`
  class in `TOOL_SCHEMAS` (the model sees this; without it the tool has no parameters), a tier in
  `BASE_TOOL_RISK_MAP`, and its name in `WORKSPACE_BOUND_TOOLS` if it works on project files. The first paragraph of
  the docstring becomes the tool description.
- **The prompt head is a KV-cache contract.** The system message must be byte-identical turn to turn. Per-turn
  material (time, skill text, RAG chunks) rides on the current user turn.
- **Confirmation is decided per call.** A tool's description must not say whether it asks.
- **Ephemeral turns** never write to SQLite. A new per-session write needs a method on `EphemeralMemoryStore`.
- **Tests** point the database and workspaces at temp directories. Run them with
  `.\.venv\Scripts\python.exe -m pytest` from the repo root.

## 4. Working with the user

- The user directs and tests on the device; say what is verified by build or test and what is seen working on screen.
- Ask before every commit. Keep unrelated working-tree files out of commits unless asked.
- Anything unconfirmed on the device is recorded as "unverified on device" in `PLAN.md`.
- After a milestone lands, move it from `PLAN.md` §4 to `docs/MILESTONES.md` and update §4.
- UI changes update the design system artifact in the same piece of work (see CLAUDE.md).
