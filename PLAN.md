# Jarvis Assistant — System Master Plan & Architecture Roadmap

**Stack:** FastAPI + native `llama-server.exe` (Qwen3.5-9B / Qwen3.5-4B) + WinUI 3 native client with a D3D11 liquid-glass renderer (`desktop-winui/`) + OpenRouter (Heavy Mode)  
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
│  │   WinUI 3 Native Client       │     │  Deprecated: Next.js/Tauri and  │  │
│  │   (desktop-winui/, HTTP+SSE)  │     │  pywebview UIs (not yet deleted)│  │
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
| **Frontend UI** | **WinUI 3 / C# (`desktop-winui/`, `Jarvis.slnx`)** | Native shell (`Jarvis.App`) + API/SSE client (`Jarvis.Core`) + liquid-glass renderer (`Jarvis.Glass`: D3D11 glass in a `SwapChainPanel` under the transparent WinUI window's XAML tree, live desktop capture, layered refraction/specular shaders, glass controls). `desktop-app/` (Next.js/Tauri) and `desktop/` (pywebview) are deprecated. |
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
- [x] **Phase 8 — Next.js Desktop Application** (superseded): `desktop-app/` was the canonical client until the WinUI 3 migration; kept on disk, deprecated.
- [x] **Phase 10 — WinUI 3 native client + liquid glass (2026-09-14)**: `desktop-winui/` replaces the web client. `Jarvis.Glass/Liquid/` puts a D3D11 glass `SwapChainPanel` under any WinUI window's content (`GlassHost`), renders per-layer displacement → refraction → specular-rim passes over a live `Windows.Graphics.Capture` of the desktop, and ships glass controls (`GlassToggle`, `GlassSlider`, `GlassButton`, `GlassSegmented`, `GlassTextField`) plus DirectWrite text on the glass layers. Settings is an inline glass pane; controls nested in a slab/ScrollViewer clip and layer correctly and stay in step with scrolling (`GlassScroll`). Slabs cast shadows; the glass moves with the window in the same DWM frame (SwapChainPanel, 2026-09-15); rendering is change-driven with per-layer output caching, scissored control layers and reduced-res frost (~6% GPU idle under a changing desktop).
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

- **Glass: message bubbles and the session list** are still stock XAML, not glass.
- **More proactive actions**: extend `AwarenessMonitor.actions` beyond VRAM eviction — e.g. nudge or close a heavy external app after a sustained `heavy_external_app` observation, or flag largest files/artifacts when `disk_space` goes critical.
- **Confirmation timeout voice feedback**: `CONFIRMATION_TIMEOUT_ACTION` in `permissions.py` silently denies an unanswered action; a hands-free session that never gets a yes/no should hear that it timed out rather than just going quiet.
- **Wire the projector into a model selection**: `models/unsloth/Qwen3.5-4B-MTP-GGUF/mmproj-F32.gguf` is now discovered and reported by `GET /api/models` under `projectors`, but nothing passes `--mmproj` yet, so images in attachments are stored rather than seen. The catalogue is the natural place to pair a projector with the model it belongs to.
- **Vision model is present but unwired**: `models/unsloth/Qwen3.5-4B-MTP-GGUF/` ships an `mmproj-F32.gguf` projector alongside the 4B, so the fast model can take images, but `VISION_MODEL` is empty and `RuntimeProcessManager` never passes `--mmproj`. Wiring it would make attachments with images actually legible to the model rather than just stored.
