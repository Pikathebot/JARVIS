# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Jarvis is a private, local-first AI assistant for Windows 11. A FastAPI backend drives native
`llama-server.exe` (llama.cpp) inference (Qwen3.5-9B main / Qwen3.5-4B fast, full GPU offload on an
8GB RTX 4060), with tool calling, deterministic safety permissions, a hardware resource governor,
project workspaces with versioned artifacts, RAG, MCP, voice, and proactive/ambient behavior.

**The frontend is `desktop-winui/`** (WinUI 3 / C#, solution `Jarvis.slnx`) — the only client;
the earlier Next.js/Tauri and pywebview UIs and the `run_jarvis.py` launcher were deleted in
September 2026, so don't look for them. The app supervises the backend process itself
(`BackendHost`). The backend (`http://127.0.0.1:8000`) is UI-agnostic — the seam to any frontend
is pure HTTP + SSE, so backend changes should not assume a particular client.

## Commands

**Backend (from repo root — `pytest.ini` sets `pythonpath = backend .`):**
```powershell
.\.venv\Scripts\python.exe -m pytest                          # full suite
.\.venv\Scripts\python.exe -m pytest backend/tests/test_x.py   # single file
.\.venv\Scripts\python.exe -m pytest backend/tests/test_x.py::test_name  # single test
cd backend; ..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```
`asyncio_mode = strict` — async tests need explicit `@pytest.mark.asyncio`.

**Launch full app (backend + UI):**
```powershell
.\"Start Jarvis.bat"            # → scripts/start-jarvis.ps1: build if needed, register dev package, launch by AUMID
.\"Start Jarvis.bat" -Build     # force a rebuild first
```
The app finds the repo root by walking up from its exe (`backend/app/main.py` + `desktop-winui/`)
and starts uvicorn from `.venv` inside a Job Object, so no separate backend launch is needed.

**WinUI native client** (from `desktop-winui/`):
```powershell
dotnet build Jarvis.slnx
dotnet run -c Debug --no-build --project Jarvis.App
```
Kill any running `Jarvis.App` process first (`Get-Process -Name 'Jarvis.App' | Stop-Process -Force`)
— a live instance locks the AppX output and `dotnet run` fails with an unhelpful MSB3027.

## Architecture

### Backend layout (`backend/app/`)
- `main.py` — FastAPI app, router mounting, startup migrations.
- `config.py` — Pydantic Settings; env vars documented in `backend/.env.example`.
- `agent/` — the agent loop:
  - `orchestrator.py` — multi-turn tool-calling loop, attachment injection, SSE event emission.
  - `runtime_process_manager.py` — spawns/manages `llama-server.exe` (per-slot model paths, extra
    args, `--no-mmap`); consults `model_catalog.py` before configured `.env` paths so a runtime
    model choice (`data/models.json`) survives restarts.
  - `llamacpp_provider.py` — OpenAI-compatible client to llama-server; `ThinkTagStreamScanner`
    splits `<think>` reasoning out of the token stream into separate `reasoning_delta`/`text_delta`
    SSE events (reasoning is a distinct channel, not inline text — see `messages.reasoning_content`).
  - `model_router.py` — local vs. OpenRouter "Heavy Mode" routing. Tools are selected *before*
    routing; a turn offering any non-LOW_RISK tool is pulled onto `main` (`_prefer_capable_slot`).
    To see exactly what a model was sent, set `LLAMA_PAYLOAD_DUMP_DIR` — every request lands there
    as JSON, replayable with curl against `:8001`.
  - `permissions.py` — deterministic O(1) safety tiers (`LOW_RISK` / `CONFIRMATION_REQUIRED` /
    `HIGH_RISK`); confirmation-required actions get a SHA-256 `act_<hash>` token the user must
    approve, and can carry a TTS-ready `spoken` prompt for voice-initiated turns.
  - `tools/` — built-in native tools (file I/O, patch, web, OS control, etc.), registered via
    signature introspection (`validator.py`).
- `governor/` — Hardware Resource Governor V2: PyNVML GPU/VRAM + psutil CPU/RAM telemetry,
  threshold-based request throttling (`resource_governor.py`, `process_watcher.py`).
- `awareness/` — `AwarenessMonitor` is the **single shared channel** for anything Jarvis says
  unprompted (hardware warnings, scheduled briefings, proactive tool actions). Rules over a
  telemetry snapshot produce `Observation`s; the monitor owns all restraint (announce once,
  escalate through cooldown, quiet on de-escalation, announce recovery once). A kind of observation
  can carry a registered action (e.g. VRAM-critical evicts the model itself). **Any new
  "Jarvis says/does something on its own" feature should emit through `monitor.emit(...)`
  rather than inventing new SSE/voice plumbing** — the frontend already renders/speaks anything
  delivered this way.
- `routines/` — scheduled briefings/messages, persisted to `data/routines.json`, delivered through
  the awareness monitor's own channel.
- `persona/` — `jarvis` / `assistant` / `operator` profiles; persona changes manner (address term,
  voice, tone, spoken-length cap) via a system-prompt preamble, never the tool protocol itself.
- `database/` — SQLModel models + session factory; schema changes go through Alembic
  (`backend/alembic/versions/`), auto-applied on FastAPI startup.
- `memory/` — `MemoryStore` (sessions/messages) and progressive context compaction.
- `mcp/` — JSON-RPC 2.0 stdio client/manager for external MCP tool servers.
- `skills/` — dynamic Markdown+YAML-frontmatter skill loader (`skills/*.md`), trigger-keyword matched.
- `voice/` — wake-word spotting, TTS text sanitization; the browser/client does mic capture and VAD
  locally and uploads whole utterances — the backend decides what an utterance *means* (wake vs.
  follow-up-window vs. barge-in), not the client.
- `rag/` — local CPU embeddings + hybrid keyword/vector retrieval, scoped per project workspace.
- `routers/` — one APIRouter module per resource area (projects, artifacts, models, persona,
  routines, awareness, voice, ...).

State that must survive restarts (persona overrides, model selection, routines) follows one
pattern throughout: a small JSON file under `data/` (gitignored), loaded in `__init__`, rewritten
on every mutation — see `persona/manager.py` or `routines/scheduler.py` before inventing a new one.

### Project workspaces
Each project gets a directory tree under `${WORKSPACE_PATH}/projects/{project_id}/`
(`files/`, `knowledge/`, `artifacts/`, `memory/`, `indexes/`), managed via `/api/projects`. User
attachments upload into `files/` and are tracked in the `attachments` table; AI-produced artifacts
are versioned (`artifacts` + `artifact_versions` tables).

### Model selection
Models are enumerated from whatever GGUFs exist under `models/` (`agent/model_catalog.py`), not
hardcoded — a per-slot (`main`/`fast`) choice persists to `data/models.json` and is consulted ahead
of `.env`'s `LLAMA_MAIN_MODEL_PATH`/`LLAMA_FAST_MODEL_PATH`. Selecting a model restarts
`llama-server` onto it, including sweeping any server that was started outside Jarvis. Don't assume
the model paths in `.env`/`backend/.env.example` reflect what's actually configured at runtime —
check `data/models.json` and `GET /api/models`.

### WinUI client (`desktop-winui/`)
Three projects: `Jarvis.Core` (DTOs mirroring the backend's JSON contracts, `JarvisApiClient`,
SSE readers), `Jarvis.Glass` (the liquid-glass rendering system — panels, wallpaper/live-capture
backdrops, Win2D shader layer), `Jarvis.App` (the shell: `MainWindow`, `HudWindow`, view models,
services). Talks to the backend purely over HTTP/SSE.

Known gotchas (see memory `winui_migration_status` for full detail if working in this area):
- JSON wire contract requires `JsonNamingPolicy.SnakeCaseLower` on `JarvisJson.Options` — case-
  insensitive matching alone is not enough (`session_id` vs `SessionId`).
- `x:Bind` with a `Converter=` does not compile when the XAML root is `Window` (unlike WPF, WinUI's
  `Window` isn't a `FrameworkElement`); do converter-driven binding in code-behind instead. Same
  restriction applies to binding non-`FrameworkElement` targets like `ColumnDefinition.Width`.
- To screenshot the running app: `PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT)` for the window's
  own content; `CopyFromScreen` is unreliable (grabs whatever's on top at those coordinates, and
  can't be trusted to have actually brought the target window to the foreground first) — but
  `PrintWindow` also can't see a `SystemBackdrop`, so for judging glass/backdrop rendering use a
  foreground-asserted `CopyFromScreen` instead (bring window to front, verify
  `GetForegroundWindow() == target`, abort if not, then capture).

## Tests
`backend/tests/` (200+ tests), run via `pytest` from repo root. `norecursedirs` excludes
`jarvis_project`, `.venv`, `desktop-winui`, `dist`, `build`.
