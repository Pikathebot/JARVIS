# Jarvis — System Architecture

> Current as of 2026-10-25. For working rules and the contracts that are easy to break, read `CLAUDE.md`.
> For what is built and what is next, read `PLAN.md`; the history is in `docs/MILESTONES.md`.

Jarvis is a private, local-first assistant for one Windows 11 laptop (i7-14700HX, RTX 4060 Laptop 8 GB, 16 GB RAM).
A FastAPI backend drives a native `llama-server.exe` (llama.cpp). A native WinUI 3 client is the only frontend.

---

## 1. Process layout

```
 WinUI 3 app (desktop-winui/: Jarvis.App, Jarvis.Core, Jarvis.Glass)
   │  starts and supervises uvicorn in a Job Object (BackendHost)
   │  HTTP + SSE only
   ▼
 FastAPI backend :8000   (backend/app/main.py, cwd = backend/)
   ├── llama-server :8001  main chat model (Ornith 1.5 9B IQ3_XXS + mmproj, 32k ctx)
   │                       fast model (MiniCPM5-2B Q4_K_M, text-only) runs under the same process
   ├── captioner    :8002  CPU llama-server, only for models without vision
   ├── embedder     :8003  CPU llama-server (nomic-embed-text v1.5) for RAG
   ├── Laya         :8004  GPU Python sidecar, voice mode only (tools/laya-venv)
   ├── remote       :8005  the phone/iPad port, loopback only, reached through `tailscale serve`
   └── Bark         :8006  speech-to-text sidecar (Python, CPU or CUDA, stopped when idle)
```

Sidecars are cold-started on first use and stopped after 300 s idle. Ports 8001–8003 are CPU or GPU llama-servers
launched by `runtime_process_manager.py`; a second llama-server on an occupied port binds silently on Windows.

## 2. Models and where they are chosen

- The served models come from `data/models.json` (per slot, set in Settings → Models) ahead of `backend/.env`.
  `GET /api/models` is the runtime truth. Defiant (`models/defiant-fable-9b/`) is the fallback main model.
- A projector is paired only if its file name starts with `mmproj` (`PROJECTOR_PREFIXES`).
- Ternary quants launch on the PrismML fork (`tools/llama-cpp-prism/llama-server.exe`), if one is selected.
- The model router (`agent/model_router.py`) gives a verdict per turn. A "fast" verdict never swaps the loaded model;
  it runs on whatever is loaded with thinking off, and gets no tools.
- OpenRouter "Heavy Mode" exists behind `CLOUD_ROUTING_ENABLED` and is off. On failure the local path runs.

## 3. Request path

`POST /chat/stream` (`main.py`) → `AgentOrchestrator.run_stream` (`agent/orchestrator.py`):

1. Session and project resolved; skills matched by keyword triggers (`skills/loader.py`).
2. The tool set is fixed per space (`tools_for_space()` in `agent/tools/registry.py`). Freeform gets every tool not in
   `WORKSPACE_BOUND_TOOLS`; Workspace gets all of them. The list and order are the same every turn.
3. The model router verdict, with the image and loaded-slot overrides.
4. RAG (Workspace only, behind a relevance floor), then `memory/context_manager.py` builds the messages.
5. The provider loop (`_run_provider_stream_loop`, `llamacpp_provider.py`) streams the reply, splits `<think>` into
   `reasoning` events, and runs tool calls through `agent/permissions.py` and then `agent/tool_pipeline.py`
   (worker thread, per-tool timeout, output truncation). It loops until a final answer.
6. SSE events go to the client (see `API_REFERENCE.md`). A confirmation ends the stream without `done`; the turn
   resumes from `agent/paused_turns.py` when the card is answered.

## 4. Spaces and ephemeral turns

- **Freeform** (`chat_mode=FREEFORM`): no project, no RAG, no workspace-bound tools. File tools are rooted at the
  user's home folder (`paths.freeform_root()`). One continuing thread is kept by the thread keeper (`thread_live.py`,
  `routers/thread.py`).
- **Workspace** (`chat_mode=WORKSPACE`): projects under `backend/workspace/projects/{id}/` with `files/`, `knowledge/`,
  `artifacts/`, `memory/`, `indexes/`, a `PROJECT.md` state file (`project_state.py`) and a git repository.
- `ephemeral=true` builds the orchestrator over `memory/ephemeral.py`: history lives in memory and nothing reaches
  SQLite. Each new per-session write must be added to that store too.

## 5. Safety

- **Permissions** (`agent/permissions.py`): every tool has a tier in `BASE_TOOL_RISK_MAP`. Tiers are argument-aware
  (paths inside or outside the workspace, commands, `launch_app`, `kill_process`, URLs). Unlisted tools need
  confirmation. `CONFIRMATION_REQUIRED` and `HIGH_RISK` are handled the same way: both need an approval token.
- **Tokens**: `act_<hash>` is a SHA-256 of tool and normalised arguments. `agent/confirmations.py` adds a 90 s expiry.
- **Taint** (`agent/taint.py`): after the turn reads outside content (web pages, fetched files), actions that could
  leak or change state need a fresh approval.
- **File safety**: writes go through `agent/tools/file_safety.py`, which backs up a file before changing it.

## 6. Governor

`governor/` watches the whole card through NVML and psutil, with per-process GPU counters. It evicts the model when
other processes grow VRAM relative to a baseline taken at launch, or when real external compute appears. Our own
llama-server is exempt through its `/slots` endpoint. A model launch walks a ladder (full, no projector, half context,
fast model under main's name) sized to the free VRAM, and announces any downgrade through the awareness channel.
Game mode (`/api/game-mode`) unloads everything before a game starts.

## 7. Awareness, routines and persona

- Anything Jarvis says unprompted goes through `AwarenessMonitor.emit(Observation)` (`awareness/monitor.py`), which
  owns restraint: announce once, escalate, recover once. Clients render and speak whatever arrives on that channel.
- `routines/scheduler.py` runs scheduled routines; `persona/manager.py` holds the manner and overrides.
- Both keep their state in a small JSON file under the repo-root `data/`, rewritten on every change.

## 8. Voice

- Speech in: Bark 3.0 Pro (`tools/bark-pro`, a Parakeet-TDT fine-tune, English only) through `scripts/bark_server.py`
  in `tools/laya-venv`. faster-whisper `small.en` on CPU is the fallback, and it answers until Bark is ready.
- Speech out: Kokoro-82M on CPU. edge-tts only with `VOICE_TTS_BACKEND=edge`.
- Voice mode (`/api/voice/mode`) starts Laya on the GPU; the wake word, follow-up window and barge-in live in
  `voice/session.py`.

## 9. Storage

| What | Where |
|---|---|
| Chat, sessions, projects, artifacts, memories (SQLite, SQLModel + Alembic, migrations at startup) | `backend/data/jarvis_memory.db` |
| Models, persona, routines (JSON, restart-surviving) | `data/` at the repo root |
| Workspaces | `backend/workspace/projects/{id}/` |
| RAG index | FTS5 in the database, plus local Qdrant |
| Logs | `backend.log` (repo root); `launcher-winui.log`; the AppX logs under `desktop-winui/.../AppX/` |

Tests point the database and workspaces at temp directories (`backend/tests/conftest.py`).

## 10. Client (`desktop-winui/`)

- **Jarvis.Core**: DTOs that mirror the backend JSON, `JarvisApiClient`, the SSE readers and the typed/spoken
  yes/no parser. `JarvisJson.Options` must keep `JsonNamingPolicy.SnakeCaseLower`.
- **Jarvis.App**: `MainWindow` (sidebar, chat, composer, right panel, inline sheets), `HudWindow` (Ctrl+Shift+J),
  view models, `BackendHost`, `VoiceViewModel`, the phone/iPad remote and web push (`remote_push.py`).
- **Jarvis.Glass**: the D3D11 Liquid Glass renderer. `GlassHost` puts a premultiplied SwapChainPanel under the XAML
  tree; each layer samples the finished layer below. Controls publish shapes to a `GlassScene` every tick.
- **Jarvis.GlassLab**: a standalone tuning bench with its own copies of the controls and shaders. It is not in
  `Jarvis.slnx`.

Build and launch through `Start Jarvis.bat` (see `CLAUDE.md`). A plain `dotnet build` does not refresh the registered
AppX layout.
