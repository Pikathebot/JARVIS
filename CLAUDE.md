# CLAUDE.md

Guidance for Claude Code in this repository. Rewritten 2026-09-23 from a read of the code itself;
where this file and older docs disagree, trust this file and `PLAN.md`, then verify in code.

## What this is

Jarvis is a private, local-first AI assistant for one Windows 11 laptop (i7-14700HX, RTX 4060
Laptop **8 GB**, 16 GB RAM). A FastAPI backend drives native `llama-server.exe` (llama.cpp) with
tool calling, deterministic permission gating, a hardware governor that keeps the model inside
the VRAM the desktop leaves free, project workspaces with RAG and versioned artifacts, local voice
(Whisper in, Kokoro out) and unprompted "ambient" remarks. The only client is a native WinUI 3 app
(`desktop-winui/`) with a hand-built D3D11 Liquid Glass renderer modelled on iOS/iPadOS 26.

The whole project has been written by Claude sessions; the user directs, tests on the device and
reports back. `PLAN.md` is the roadmap (§4 = what's next); `docs/MILESTONES.md` is the detailed
log of what was built and why. Everything else in `docs/` (ARCHITECTURE, AI_AGENT_CONTEXT,
API_REFERENCE, Build plan, the GOVERNOR/STAGE specs) is **historical** -- written before the
WinUI port and the model changes, and wrong about models, clients and paths in places.

## Commands

Backend, from the repo root (`pytest.ini` sets `pythonpath = backend .`, `asyncio_mode = strict`
so async tests need `@pytest.mark.asyncio`):
```powershell
.\.venv\Scripts\python.exe -m pytest                                   # ~2 min; 724 pass (2026-10-02)
.\.venv\Scripts\python.exe -m pytest backend/tests/test_x.py::test_name
cd backend; ..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Full app -- this is how the user runs it, and the only launch that is reliably current:
```powershell
.\"Start Jarvis.bat"          # -> scripts/start-jarvis.ps1: kill stale app, rebuild if sources are
.\"Start Jarvis.bat" -Build   #    newer than the AppX layout, register dev package, launch by AUMID
```
The app finds the repo root from its own exe and starts uvicorn itself (`BackendHost`, in a Job
Object, cwd `backend/`), so no separate backend launch is needed.

WinUI, from `desktop-winui/`:
```powershell
Get-Process -Name 'Jarvis.App' -ErrorAction SilentlyContinue | Stop-Process -Force   # else MSB3027
dotnet build Jarvis.slnx            # App + Core + Glass only
dotnet build Jarvis.GlassLab        # NOT in the solution -- build it separately after touching it
```
A plain `dotnet build` does **not** refresh the registered AppX layout; launching afterwards by
AUMID runs the previous build. Use the launcher script, which detects this and rebuilds.

## Runtime reality (check before trusting config)

- **Models actually served** come from `data/models.json` (per-slot selection) ahead of `.env`.
  As of 2026-09-23: main = `models/qwen3.5-9b/Qwen3.5-9B-UD-IQ3_XXS.gguf` (+ Q8_0 projector, 32k
  ctx, ~5.0 GB), fast = `models/minicpm5-2b/MiniCPM5-2B-Q4_K_M.gguf` (text-only). `backend/.env`'s
  `LLAMA_*_MODEL_PATH` / `MAIN_MODEL` still name the older Q3_K_XL and 4B -- stale, not live.
  `GET /api/models` is the truth at runtime.
- **Ports:** 8000 backend, 8001 llama-server (the chat model), 8002 image-captioner sidecar,
  8003 embedding sidecar. Both sidecars are CPU-only llama-servers launched with
  `CUDA_VISIBLE_DEVICES=-1`, cold-started on first use and stopped after 300 s idle.
- **State paths** (the backend's cwd is `backend/`, so relative paths resolve there):
  `data/{models,persona,routines}.json` at the **repo root** (resolved from `__file__`);
  the live SQLite DB is `backend/data/jarvis_memory.db`; live workspaces are
  `backend/workspace/projects/{id}/`. Tests are pointed at temp dirs for the DB and workspaces
  (`backend/tests/conftest.py`), so a run leaves nothing in the repo.
- **Logs:** `backend.log` (repo root) for the backend; `launcher-winui.log` for the launcher;
  `AppX\jarvis-glass.log` and `AppX\jarvis-app-crash.log` under
  `desktop-winui\Jarvis.App\bin\Debug\net10.0-windows10.0.26100.0\win-x64\AppX\`.
- **Payload replay:** set `LLAMA_PAYLOAD_DUMP_DIR` and every request to llama-server is written as
  JSON, replayable with curl. Best tool for "why did the model do X".

## How a chat turn flows (backend)

`POST /chat/stream` (`main.py`) -> `AgentOrchestrator.run_stream` (`agent/orchestrator.py`):
1. Session + project resolved; skills matched (`skills/loader.py`, keyword triggers).
2. **A fixed tool set per space**: `tools_for_space()` in `agent/tools/registry.py` -- Freeform
   = every tool not in `WORKSPACE_BOUND_TOOLS`, Workspace/System = all; same list and order
   every turn (keeps the prompt prefix byte-stable). No keyword picking since 2026-10-01.
3. `ModelRouter.evaluate` (regex heuristics) gives a verdict, then two overrides:
   `_prefer_seeing_slot` (images -> a slot with vision), `_prefer_loaded_slot` (a "fast" verdict
   never swaps models: it runs on whatever is loaded, thinking off per request -- except that a
   message `looks_like_action` never runs on the fast model). A turn that really runs on the
   fast model gets **no tools** (plus a note to say so if asked to act).
4. RAG (WORKSPACE mode only, gated by `should_retrieve` and a relevance floor), then
   `ContextManager.build_context` assembles messages.
5. Provider loop (`_run_provider_stream_loop`, `llamacpp_provider.py`): stream, split `<think>`
   into `reasoning` events, run tool calls through `permissions.py` then `tool_pipeline.py`
   (worker thread, per-tool timeout, output truncation), loop until a final answer.
6. SSE events to the client: `token`/`text_delta`, `reasoning`, `tool_draft`/`tool_call`,
   `tool_start`/`tool_end`, `retrieval_context`, `confirmation_required`, `done`, `error`.
   A confirmation **ends the stream without `done`**.

OpenRouter "Heavy Mode" exists but is off (`CLOUD_ROUTING_ENABLED=false`); on failure the local
path runs instead.

## Contracts that are easy to break

- **Prompt prefix is a KV-cache contract.** The head system message (persona preamble +
  `TOOL_PROTOCOL_RULES` + project instructions + the tool schemas the template renders there) must
  be byte-identical turn to turn. Anything per-turn (current time, skill text, RAG chunks) goes
  through `build_turn_context()` and rides on the *current user turn*, never stored. Qwen3.5's
  template rejects a second system message. Breaking this silently re-prefills the whole
  conversation every turn (measured 1363->1642 tokens vs 112-198). Pinned by
  `test_per_turn_material_rides_on_the_user_turn_not_the_prefix`.
- **Two spaces, and ephemeral turns.** `chat_mode=FREEFORM` = no project (not even the active
  one), no RAG, no workspace-bound tools (`WORKSPACE_BOUND_TOOLS` in `registry.py`: not offered,
  and refused by `execute_tool`); its sessions are listed apart (`/sessions?chat_mode=FREEFORM`).
  `ephemeral=true` builds the orchestrator over `EphemeralMemoryStore` (`memory/ephemeral.py`):
  history lives in process memory, nothing reaches SQLite, `DELETE /sessions/{id}` purges. A new
  per-session write in the turn path must be added to that store too -- it raises on any
  `MemoryStore` method it hasn't vetted rather than fall through to the database.
- **Anything Jarvis says unprompted goes through `AwarenessMonitor.emit(Observation)`**
  (`awareness/monitor.py`). The monitor owns restraint (announce once, escalate, recover once) and
  the client already renders and speaks whatever arrives on that channel. Don't add new SSE/voice
  plumbing for a proactive feature.
- **Restart-surviving state** = a small JSON file under repo-root `data/`, loaded in `__init__`,
  rewritten on every mutation (`persona/manager.py`, `routines/scheduler.py`). DB schema changes go
  through Alembic (`backend/alembic/versions/`), auto-applied at startup.
- **Adding a tool the model can call** means all four of, in `agent/tools/registry.py` unless
  noted: the function in `TOOL_FUNCTIONS` and `AVAILABLE_TOOLS`; a Pydantic `*Args` class in
  `TOOL_SCHEMAS` (**this is the schema the model sees** -- a tool without one is offered with no
  parameters at all; `execute_tool` then filters the call's args by the function signature and
  injects `workspace_path`/`project_id`/`session_id`, plus `user_message` and `allow_outside`,
  which a model can never supply); a tier in `BASE_TOOL_RISK_MAP`
  (`agent/permissions.py`; unlisted = CONFIRMATION_REQUIRED); and, if it works on project
  files, its name in `WORKSPACE_BOUND_TOOLS` (else Freeform offers it too). File tools resolve
  paths through `agent/tools/paths.py` (outside the workspace only when the gate checked the
  call) and back up before changing a file (`file_safety.py`). The docstring's **first paragraph** (up to a blank
  line or `Args:`, joined onto one line -- `tool_description` in `tool_schema.py`) becomes the
  tool description, so any "only when asked" caution must be in it. Don't say there whether the
  tool asks for confirmation: the permission gate decides that per call. Tools are sync
  functions returning a string (errors as `"Error: ..."` text); they run on a worker thread.
  `agent/tools/git.py` is a compact example.
- **Permissions:** `LOW_RISK` runs; `CONFIRMATION_REQUIRED` and `HIGH_RISK` are handled
  *identically* (both need an approval token; nothing is hard-blocked). Tiers are argument-aware
  (`execute_command`, file paths, `launch_app`, `kill_process`, URLs). The `act_<hash>` token is a
  deterministic SHA-256 of tool+normalised args; `agent/confirmations.py` adds expiry
  (`CONFIRMATION_TIMEOUT_SECONDS`, 90) and refuses late approvals.
- **The governor watches the whole card.** It evicts the model when *other* processes grow VRAM
  (relative to a baseline taken at launch) or real external compute appears; our own llama-server
  decoding is exempted via its `/slots` endpoint. Model launch walks a ladder (full -> no
  projector -> half context -> fast model under main's name) sized to free VRAM, and announces
  any downgrade through the awareness channel.

## Traps in the backend

- **There is one tool system**, `app/agent/tools/`. A second `BaseTool` stack (`app/tools/`,
  `AgentLoop`, sandbox/vision/search packages), the Ollama/LM Studio fallback providers, the
  reliability "rollback" monitor and the backend-side Chatterbox speech were all registered but
  never reachable, and were deleted on 2026-09-23 (git tools and ranged `read_file` were ported
  first). Older docs and `.env` files may still mention them; `Settings` ignores unknown keys.
- Orchestrator tests inject a model through `AgentOrchestrator(provider=...)` or, for tests that
  go through the HTTP endpoints, `use_model_client(fake)` from `backend/tests/fake_providers.py`.
- Hitting `:8001` directly while the backend runs is fine for compute now, but a second model
  load or a big VRAM grab from a script still reads as an external workload.
- `0xC0000409` at llama-server start = CUDA lazy module load failing under VRAM pressure, not a
  flag problem. The launch ladder handles it; `CUDA_MODULE_LOADING=EAGER` is the manual workaround.
- On Windows a second llama-server binds an occupied port *silently*. If a model switch seems to
  do nothing, look for a stale `llama-server.exe` (children are in a KILL_ON_JOB_CLOSE job now).

## WinUI client (`desktop-winui/`)

- `Jarvis.Core` -- DTOs mirroring the backend JSON, `JarvisApiClient`, SSE readers,
  `ConfirmationIntentParser` (typed/spoken yes/no). `JarvisJson.Options` must keep
  `JsonNamingPolicy.SnakeCaseLower`; case-insensitive matching alone breaks every request.
- `Jarvis.App` -- `MainWindow` (sidebar, chat, composer, right panel, inline sheets in
  `SettingsHost`), `HudWindow` (Ctrl+Shift+J overlay with its own voice session), view models,
  `BackendHost`, `VoiceViewModel` (AudioGraph mic, client-side VAD, whole utterances to
  `/api/voice/listen`; the backend decides what an utterance means).
- `Jarvis.Glass` -- the renderer. `GlassHost` puts a premultiplied `SwapChainPanel` at the bottom
  of a window's XAML tree; `LiveCaptureSource` captures the desktop (the window is excluded from
  capture); `GlassRenderer` draws per **layer**: displacement field -> refraction -> specular rim,
  each layer sampling the finished layer below, optional Gaussian frost pre-pass, DirectWrite text
  composited per layer. Controls (`GlassToggle`, `GlassSlider`, `GlassSegmented`, `GlassButton`,
  `GlassTextField`) and panels (`GlassSlab`) publish shapes to the window's `GlassScene` each tick.
- `Jarvis.GlassLab` -- standalone tuning bench with **its own copies** of the controls and
  shaders. Material/interaction fixes are usually applied to both copies; it is not in
  `Jarvis.slnx`.

Glass rules learned the hard way:
- Layers come from `GlassLayers` (named constants), never a bare integer in XAML. A shape only
  covers and frosts what is on a *lower* layer; same-layer shapes draw in arbitrary order.
- XAML always paints **above** the swapchain, so glass can never frost XAML text; sheets fade the
  window's XAML instead (`SetSheetOpen`). Popups/`ContentDialog`/`Flyout` paint above the window,
  so glass controls inside them are invisible -- use inline panes.
- `ReleasePointerCapture` raises `PointerCaptureLost` synchronously: read drag state before
  releasing. Taps and drags are separated by a max-distance tap slop, not by "moved at all".
- `x:Bind` with `Converter=` (or targeting `ColumnDefinition.Width`) doesn't compile under a
  `Window` root -- do it in code-behind.
- Material constants are measured, not guessed: the reference is the user's iPad recording (see
  memory `liquid_glass_reference`); measure frames numerically, never trust a video model.
- **Screenshots: `scripts\snapshot-window.ps1`** (-OutDir, -Window main|hud|all, -DelayMs), then
  Read the PNG. The windows stay excluded from capture (`WDA_EXCLUDEFROMCAPTURE`); never just turn
  that off -- a capturable window's glass captures its own output, renders every frame (~70% GPU)
  and the governor evicts the model, and the shot shows glass refracting itself. The script asks
  the app's `SnapshotService` (polls for `snapshot.request` beside the exe) to freeze the glass
  and hold every spring (`GlassHost.Frozen`, `GlassMotion.Paused`), lift exclusion on the
  *visible* windows only (a hidden HUD made capturable showed up as a white slab), and report
  their rects; it copies the pixels and the app re-excludes and resumes (~0.5 s; self-releases
  after 10 s). The main window is only captured while it is the foreground window (a covered
  window would yield whoever covers it). Needs a running build that has SnapshotService
  (2026-09-24+). The glass shows the user's desktop blurred behind it -- keep shots in the
  scratchpad. Still ask the user about motion and feel; a still can't show those.
  Each snapshot also writes `snapshot.shapes.txt` beside the exe: every published glass shape
  with its owner, centre/half size (px), layer, clip and fade (`GlassScene.Describe`). When a
  still shows glass in the wrong place, read it first -- it splits "published wrong" from
  "rendered wrong".

## Working with the user

- Ask before every commit, with a short summary of what was verified. Keep unrelated working-tree
  files (`.agents/`, `Jarvis.lnk`) out of commits unless asked.
- The user verifies on the device; say plainly what is build/test-verified versus seen working on
  screen, and record anything unconfirmed as "unverified on device" in `PLAN.md`.
- After finishing a milestone, move it from `PLAN.md` §4 into `docs/MILESTONES.md` (with the why)
  and update §4 so the next session starts from accurate state.
