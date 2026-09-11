# Jarvis Assistant — System Master Plan & Architecture Roadmap

**Stack:** FastAPI + native `llama-server.exe` (Qwen3.5-9B / Qwen3.5-4B) + Next.js/React Desktop Client + OpenRouter (Heavy Mode)  
**Target Machine:** Windows 11, Intel i7-14700HX (20 threads), NVIDIA RTX 4060 Laptop GPU (8GB VRAM), 16GB DDR5 RAM  
**Repository:** `d:/JARVIS`  

---

## 0. Project Vision & Core Principles

Jarvis is a private, lightning-fast, hardware-governed AI assistant for Windows 11. Designed specifically for local RTX 4060 GPU offload, it provides native llama.cpp execution, process-based VRAM eviction, deterministic $O(1)$ safety permissions, project workspace switching, durable artifacts versioning, and real-time SSE streaming.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          JARVIS UNIFIED AGENT GATEWAY                       │
│                                                                             │
│  ┌───────────────────────────────┐     ┌─────────────────────────────────┐  │
│  │   Canonical Next.js Desktop   │     │  Legacy UI Fallback (pywebview) │  │
│  │   (Next.js 14 + Tailwind)     │     │  (--legacy-ui flag)             │  │
│  └───────────────┬───────────────┘     └────────────────┬────────────────┘  │
│                  │                                      │                   │
│                  └───────────────────┬──────────────────┘                   │
│                                      ▼                                      │
│                        FastAPI Orchestrator & Router                        │
│                 (Hardware Resource Governor + Safety Gating)                │
│                                      │                                      │
│  ┌───────────────────────────────────┴───────────────────────────────────┐  │
│  │                     Expanded Tools & Engine Ecosystem                 │  │
│  │                                                                       │  │
│  │  ┌─────────────────────────┐  ┌────────────────────────────────────┐  │  │
│  │  │   Web & Research Engine │  │     File & Workspace Engine        │  │  │
│  │  │   • DuckDuckGo Search   │  │     • read_file / write_file       │  │  │
│  │  │   • Fast HTML Scraper   │  │     • patch_file                   │  │  │
│  │  │   • URL Fetch & Parse   │  │     • Project Files & Attachments  │  │  │
│  │  └─────────────────────────┘  └────────────────────────────────────┘  │  │
│  │  ┌─────────────────────────┐  ┌────────────────────────────────────┐  │  │
│  │  │  Windows OS Power Tools │  │   SQLModel Relational Store        │  │  │
│  │  │   • App Launcher/Focus  │  │     • Projects (Section 7 folders) │  │  │
│  │  │   • Media & Volume Ctrl │  │     • Sessions & Messages          │  │  │
│  │  │   • Active Clipboard    │  │     • Attachments & Artifacts      │  │  │
│  │  │   • Process Management  │  │     • Artifact Versions            │  │  │
│  │  └─────────────────────────┘  └────────────────────────────────────┘  │  │
│  │  ┌─────────────────────────┐  ┌────────────────────────────────────┐  │  │
│  │  │ Voice & TTS Engine      │  │     Hardware Resource Governor     │  │  │
│  │  │   • Wake-Word Detection │  │     • PyNVML VRAM/GPU Telemetry    │  │  │
│  │  │   • Chatterbox TTS      │  │     • Adaptive Request Queue       │  │  │
│  │  └─────────────────────────┘  │     • Auto-throttle Under Load     │  │  │
│  │                               └────────────────────────────────────┘  │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 1. System Architecture Truth

| Component | Technology | Rationale |
| :--- | :--- | :--- |
| **Primary Inference Runtime** | **native `llama-server.exe` (llama.cpp)** | Maximum inference throughput on port 8001; 100% GPU offload (`-ngl 99`) and `--no-mmap` to conserve 16GB system RAM. |
| **Primary LLM Models** | **Qwen3.5-9B Q4_K_M (Main) / Qwen3.5-4B Q4_K_M (Fast)** | High intelligence, strong coding, native tool calling, fits comfortably in 8GB VRAM. |
| **Heavy Reasoning Fallback** | **OpenRouter (Cloud)** | Used for ultra-complex multi-file architectural reasoning when requested or auto-routed. |
| **Backend API** | **FastAPI + SSE (`/chat/stream`)** | High-performance asynchronous endpoint emitting token streams, tool events, and structured metadata. |
| **Database & Migrations** | **SQLModel + Alembic** | Single unified database layer (`data/jarvis_memory.db`) with automatic migrations on startup. |
| **Frontend UI** | **Next.js 14 + React + Tailwind CSS + Tauri (`desktop-app/`)** | Modern desktop UI with project workspace switcher, 4-tab RightPanel, and attachment upload. |
| **VRAM Management** | **Process-based eviction via `RuntimeProcessManager.stop()`** | Cleanly frees 100% GPU memory on demand without lingering zombie processes. |

---

## 2. Implemented Milestones (Status: Complete)

- [x] **Phase 0 — FastAPI Core & SSE Streaming**: Real-time `/chat/stream` SSE generator and `/chat` endpoints.
- [x] **Phase 1 — Native Tool Calling**: Inspect-based signature introspection, tool registration, and parameter validation.
- [x] **Phase 2 — Deterministic Safety Permissions**: $O(1)$ hardcoded security tiering (`LOW_RISK`, `CONFIRMATION_REQUIRED`, `HIGH_RISK`) with SHA256 action tokens.
- [x] **Phase 3 — Hardware Resource Governor (V2)**: PyNVML GPU/VRAM telemetry + `psutil` CPU/RAM monitoring with automatic load throttling.
- [x] **Phase 4 — Model Router & Runtime Process Manager**: Local llama.cpp process manager with `-ngl 99 --no-mmap` execution flags.
- [x] **Phase 5 — Database Unification (SQLModel + Alembic)**: Unified schema across projects, sessions, messages, attachments, and artifacts.
- [x] **Phase 6 — Project Workspaces & Section 7 Filesystem**: Directory hierarchy under `workspace/projects/{project_id}/` (`files/`, `knowledge/`, `artifacts/`, `memory/`, `indexes/`).
- [x] **Phase 7 — Artifacts, Attachments & Context Injection**: Versioned artifact storage, secure attachment uploads, and automatic LLM context turn injection.
- [x] **Phase 8 — Next.js Desktop Application**: Canonical desktop client in `desktop-app/` with workspace switcher and 4-tab RightPanel.
- [x] **Phase 9 — Context Engine & RAG**: Syntax-aware chunking, local CPU embeddings, hybrid keyword + vector retrieval, and per-project workspace scoping for tools and permissions.

---

## 3. The JARVIS Behaviour Layer

Everything above makes Jarvis *capable*. This layer is what makes it behave like Jarvis rather than a chat window with tools.

- [x] **Persona** (`backend/app/persona/`) — Three profiles (`jarvis`, `assistant`, `operator`), each with an address term, voice, tone directives and a spoken-length cap. The system prompt is composed as *persona preamble + invariant tool protocol*: a persona changes manner, never capability. Active persona and user overrides persist to `data/persona.json`.

- [x] **Hands-free voice** (`backend/app/voice/session.py`, `desktop-app/src/hooks/useVoice.ts`) — The browser owns the microphone and does local voice-activity detection, so only whole utterances are uploaded; the backend owns what an utterance *means*. A wake word dispatches immediately; a bare "Jarvis" arms the session and speaks the greeting; for 15 seconds after a reply, follow-ups need no wake word. Talking over a spoken reply cuts it off.

- [x] **Ambient awareness** (`backend/app/awareness/`) — Pure rules over a hardware snapshot (VRAM, thermals, RAM, CPU, disk, battery, model eviction, heavy external apps), with the monitor owning all restraint: announce once, escalate through the cooldown, stay quiet on de-escalation, restate at most every 5 minutes, and announce recovery exactly once. Observations stream over SSE and are spoken only while hands-free voice is on. Briefings are assembled from telemetry rather than generated, so they are instant and their numbers are always real.

- [x] **HUD overlay** (`desktop-app/src/app/hud/`, `src-tauri/src/main.rs`) — A transparent, always-on-top window summoned from anywhere with `Ctrl+Shift+J`. Voice orb, two telemetry rings, and the last thing said in either direction. It runs its own voice session so an ambient question does not interleave with the main window's work.

- [x] **Scheduled routines** (`backend/app/routines/`) — Time-of-day briefings and custom messages ("every weekday at 8am, give me the status briefing"), persisted to `data/routines.json`. The scheduler polls the wall clock and fires through the awareness monitor's own `emit()` channel, so a routine is delivered, spoken, and shown in the tray exactly like any other observation — no separate frontend plumbing needed. Managed from Settings → Scheduled Routines (`SettingsDialog.tsx`); `POST /api/routines/{id}/run` previews one immediately.

- [x] **Proactive tool use** (`AwarenessMonitor.actions`, `backend/app/main.py`) — A kind of observation can carry a registered action: when it first escalates to CRITICAL, the monitor awaits the action, then emits a follow-up observation announcing what it did. Ships with one wired case — VRAM critical now evicts the model itself (`auto_unload_models()`) rather than only warning that eviction is imminent, which fires independently of and earlier than the governor's own reactive throttle (which additionally needs high GPU compute or ≥99% raw VRAM). An action fires once per escalation, never every poll, and re-arms after recovery. Toggle: Settings → "Proactive actions" (`PATCH /api/awareness/config {actions_enabled}`), default on (`PROACTIVE_ACTIONS_ENABLED`).

- [x] **Voice-driven confirmations** (`build_confirmation_prompt` in `backend/app/agent/permissions.py`, `desktop-app/src/lib/voice-intent.ts`) — A `CONFIRMATION_REQUIRED` tool call now carries a TTS-ready `spoken` prompt ("I need your approval to run a command: git push origin main. Say yes to proceed, or no to cancel, sir.") alongside the existing approve/deny card, on both `/chat` and `/chat/stream`. The main window speaks it when the turn was voice-initiated (`page.tsx`); the HUD speaks it always, since every HUD turn is voice. A spoken "yes"/"no" (`parseConfirmationIntent`) is intercepted before it reaches the model — "yes" resubmits the original prompt with all pending action ids approved, "no" cancels, anything else re-asks rather than being treated as a new command.

- [x] **Model selection** (`backend/app/agent/model_catalog.py`, `backend/app/routers/models.py`) — the two model slots used to be pinned by `LLAMA_MAIN_MODEL_PATH` / `LLAMA_FAST_MODEL_PATH` and could only be changed by editing `.env` and restarting; there was no endpoint for it at all. The catalogue enumerates every GGUF under `models/`, remembers a per-slot choice in `data/models.json`, and is consulted by `RuntimeProcessManager` ahead of the configured paths, so a choice survives restarts without anyone hand-editing configuration. `main` (what chat runs on) and `fast` (the router's smaller fallback) are chosen independently. Models sitting directly in `models/` are marked `recommended` and returned first — the top level is where deliberately installed models live, whereas subdirectories are vendor download trees (`models/unsloth/…`) holding duplicates and companion files. `mmproj-*.gguf` projectors are reported separately and cannot be selected as chat models, since they are vision adapters loaded with `--mmproj`. Selecting activates by default, restarting llama-server onto the model — including sweeping a server that was started outside Jarvis, which otherwise keeps serving the old weights and makes the switch a silent no-op. A model that will not load keeps its selection and returns the reason rather than discarding the choice. Managed from Settings → Models.

- [x] **Structured reasoning channel + Reasoning tab (2026-09-07)** — the 4B/fast slot's thinking is now off entirely (`LLAMA_CHAT_TEMPLATE_KWARGS_FAST={"enable_thinking":false}`); the 9B/main slot keeps it on but reasoning tokens are no longer interleaved with answer tokens in the live stream. `llamacpp_provider.stream_chat()` gained `ThinkTagStreamScanner`, a stateful classifier that splits `<think>...</think>` content (tags can straddle SSE deltas) into distinct `reasoning_delta`/`text_delta` events, reconciled with the native `reasoning_content` delta field where a backend populates it. `orchestrator.run_stream` forwards these as a new `"reasoning"` SSE event and persists `reasoning_content` alongside `content` (new `messages.reasoning_content` column, migration `006`). The WinUI client has a new right-panel "Reasoning" tab (badge-only — never auto-opens) bound to `ChatViewModel.ActiveReasoningMessage`; `ChatMessage.HasStructuredReasoning` discriminates new structured messages (rendered only in the tab) from pre-feature history (still rendered via `MarkdownRenderer`'s legacy inline `<think>` `Expander`), so old sessions are unaffected.

### Key endpoints

| Area | Endpoints |
| :--- | :--- |
| Persona | `GET/POST /api/persona`, `PATCH/DELETE /api/persona/overrides`, `POST /api/persona/speech-preview` |
| Voice | `POST /api/voice/listen`, `POST /api/voice/say`, `POST /api/voice/session/{id}/{start,stop,arm,state}`, `GET /api/voice/hands-free` |
| Awareness | `GET /api/awareness/{status,observations,briefing,config,stream}`, `POST /api/awareness/poll`, `PATCH /api/awareness/config` |
| Routines | `GET/POST /api/routines`, `PATCH/DELETE /api/routines/{id}`, `POST /api/routines/{id}/run` |
| Models | `GET /api/models`, `POST /api/models/select`, `DELETE /api/models/select/{slot}` |

---

## 4. Next Milestones

- **More proactive actions**: extend `AwarenessMonitor.actions` beyond VRAM eviction — e.g. nudge or close a heavy external app after a sustained `heavy_external_app` observation, or flag largest files/artifacts when `disk_space` goes critical.
- **Confirmation timeout voice feedback**: `CONFIRMATION_TIMEOUT_ACTION` in `permissions.py` silently denies an unanswered action; a hands-free session that never gets a yes/no should hear that it timed out rather than just going quiet.
- **Live-capture glass backdrop, Checkpoint A landed (2026-09-07)**: `Jarvis.Glass/LiveCaptureService.cs` (new) captures the primary display via `Windows.Graphics.Capture` (`GraphicsCaptureItem.TryCreateFromDisplayId` + `Direct3D11CaptureFramePool`, ~12fps throttled, reference-counted `Start()`/`Stop()`), sharing one `CanvasDevice` with Win2D so the D3D11 surface wraps into a `CanvasBitmap` without a cross-device copy. Gated behind a default-OFF Settings toggle (`SettingsDialog` "Live-capture glass (experimental)", persisted via `ApplicationData.LocalSettings`) and only active at `GlassQuality.Full` (`GlassPanel.LiveCaptureRequested` opt-in per panel × `LiveCaptureAvailable` pushed in by `GlassQualityService`) — Reduced/System never start a capture session. `LiquidGlassCanvas.DrawBody` prefers the live frame over the wallpaper-file crop when available, same designed-gradient fallback otherwise. Currently opted-in on the sidebar panel only, reusing the wallpaper path's window-relative crop math (not true screen position yet). **Verify on-device before extending further**: toggle on/off via `PrintWindow`-based screenshots (not `CopyFromScreen`), confirm Reduced/System never start a capture session, and watch for the flagged empirical risks — whether `CanvasBitmap.CreateFromDirect3D11Surface` is truly zero-copy on this WindowsAppSDK/Win2D version, and whether `TryCreateFromDisplayId` needs a capture-access prompt on first use.
- **Live-capture glass backdrop, Checkpoint B (parallax) landed (2026-09-08)**: `Jarvis.Glass/WindowPositionService.cs` (new) tracks each registered top-level window's live `AppWindow` bounds (raw display pixels) via `appWindow.Changed`, registered for both `MainWindow` and `HudWindow` in `App.xaml.cs` beside the existing `GlassQuality.Register(...)` calls. `LiquidGlassCanvas.DrawBody`'s live-capture branch now resolves the owning window via `XamlRoot.ContentIslandEnvironment.AppWindowId` and crops by actual screen position (`windowBounds + panelOffset*rasterizationScale - DisplayArea.Primary.OuterBounds.Origin`) instead of only the panel's position within its own window; falls back to the prior window-relative calc if the window was never registered. The wallpaper-file source is unchanged (still window-relative only — it isn't screen-scoped content, so parallaxing it wouldn't mean anything). **Not verified visually on-device**: live capture itself doesn't produce frames on this machine yet (see Checkpoint A note below), so the screen-position crop math has been build/run-verified (no crash) but not seen actually tracking a window drag.
- **Live-capture glass backdrop, Checkpoint C (edge refraction) landed (2026-09-08)**: `Jarvis.Glass/BezelDisplacementMap.cs` (new) builds the Snell's-law 1D refraction lookup and rasterizes it into a per-panel-size RG displacement `CanvasBitmap` (bounded cache, ~24 entries, keyed by rounded width/height/radius/bezel-width) via a rounded-rect SDF + central-difference normal, exactly as planned. `LiquidGlassCanvas.DrawBody` chains a `Transform2DEffect` (resamples the blurred/saturated crop into the displacement map's own DIP-sized coordinate space) into Win2D's `DisplacementMapEffect`, gated by a new `EdgeRefractionEnabled` → `GlassPanel.EdgeRefractionRequested`/`EdgeRefractionAvailable` pair mirroring the live-capture opt-in (`GlassQualityService.Apply` reuses the same `LiveCaptureEnabled` setting rather than a second toggle). Opted in on the sidebar panel only. **Real bug found and fixed during verification**: `CanvasBitmap.CreateFromBytes` with `B8G8R8A8UIntNormalized` expects byte order Blue,Green,Red,Alpha — the first implementation wrote R,G,B,A, so the X-displacement value silently landed in the (ignored) Blue byte and `XChannelSelect.Red` always read a constant 0. Confirmed by visualizing the raw generated bitmap directly (a clear colored bezel band around a flat neutral interior, as designed) after the fix. **Still unconfirmed**: whether the composited *visual* bend is perceptible in practice — an on-device A/B diff (same static backdrop, effect on vs. off, even with the bezel/amount exaggerated 6x) showed no meaningful pixel difference beyond noise, most likely because the backdrop content reaching this effect is already heavily Gaussian-blurred (low-frequency), so a positional shift of a smooth gradient has little to show. Re-test once Checkpoint A's live-capture frames are actually flowing (real desktop content has far more high-frequency detail than a blurred static crop) before judging this checkpoint's visual payoff.
- **Live-capture glass backdrop: Checkpoint A's frames still aren't flowing on this device.** While verifying B and C, `LiveCaptureService.CurrentFrame` stayed null through every run even with the Settings toggle forced on — every panel kept falling back to the wallpaper-file crop, not live capture. `Package.appxmanifest` declares no `graphicsCapture`-family restricted capability; `GraphicsCaptureItem.TryCreateFromDisplayId`'s whole-display, no-picker capture may require one on this WindowsAppSDK/Windows build, and `LiveCaptureService.StartCaptureSession`'s broad try/catch would swallow that failure silently. This was flagged as an open empirical risk in the Checkpoint A entry above and needs to be root-caused before B/C's live-capture-specific paths can be judged on their merits.
- **Wire the projector into a model selection**: `models/unsloth/Qwen3.5-4B-MTP-GGUF/mmproj-F32.gguf` is now discovered and reported by `GET /api/models` under `projectors`, but nothing passes `--mmproj` yet, so images in attachments are stored rather than seen. The catalogue is the natural place to pair a projector with the model it belongs to.
- **Vision model is present but unwired**: `models/unsloth/Qwen3.5-4B-MTP-GGUF/` ships an `mmproj-F32.gguf` projector alongside the 4B, so the fast model can take images, but `VISION_MODEL` is empty and `RuntimeProcessManager` never passes `--mmproj`. Wiring it would make attachments with images actually legible to the model rather than just stored.
