# Jarvis AI Assistant — Master Architecture & AI Agent Onboarding Guide

> **Purpose**: This document provides incoming AI agents with a comprehensive, end-to-end overview of the **Jarvis** project — how the subsystems operate, the agent orchestrator tool loop, safety permissions, hardware governor, REST APIs, and file tree.

---

## 1. Executive Summary & Current Truth

**Jarvis** is a private, lightweight, and fast local AI desktop assistant built specifically for Windows 11 and NVIDIA RTX 4060 hardware constraints.

### Current System Truth:
1. **Primary Engine & Inference**: Native `llama-server.exe` (llama.cpp) running on `http://127.0.0.1:8001` managed by `RuntimeProcessManager`.
   - **Primary Model**: Qwen3.5-9B Q4_K_M (`llama_model_main`)
   - **Fast Model**: Qwen3.5-4B Q4_K_M (`llama_model_fast`)
   - **Execution Flags**: Full GPU offload (`-ngl 99`) with memory-mapping disabled (`--no-mmap`) to conserve system RAM.
   - **Legacy Engines**: Ollama and LM Studio are DEPRECATED and retained only for optional fallback.
2. **Process-Based VRAM Eviction**: Unloading models is handled directly via `RuntimeProcessManager.stop()` releasing 100% of GPU VRAM on demand (`POST /models/unload`).
3. **Backend**: FastAPI running on `http://127.0.0.1:8000`, featuring real-time Server-Sent Events (SSE) streaming via `POST /chat/stream` and standard `POST /chat`.
4. **Database & Migrations**: Unified SQLModel ORM + Alembic database (`data/jarvis_memory.db`), with automated migrations executed on FastAPI startup.
5. **Project Workspaces & Section 7 Filesystem**: Hierarchical workspace storage under `${WORKSPACE_PATH}/projects/{project_id}/` (`files/`, `knowledge/`, `artifacts/`, `memory/`, `indexes/`) managed via `/api/projects`.
6. **Artifacts & Attachments**:
   - **User Attachments**: Uploaded via `POST /api/upload` into project `files/` directory, tracked in `attachments` table, and automatically injected into LLM turn context.
   - **AI Artifacts**: Durable agent outputs tracked in `artifacts` and `artifact_versions` tables with full version history (`POST /api/artifacts`, `GET /api/artifacts/{id}/versions`).
7. **Desktop Frontend — mid-migration**: the active client is a native **WinUI 3 / C#** shell in [`desktop-winui/`](../desktop-winui/) (solution `Jarvis.slnx`; projects `Jarvis.App`/`Jarvis.Core`/`Jarvis.Glass`), talking to the backend purely over HTTP + SSE. The previous canonical UI, React 14 + Next.js + Tailwind in [`desktop-app/`](../desktop-app/) (built statically to `desktop-app/out/`, served via FastAPI at `/ui`), and the legacy pywebview UI in `desktop/` (`--legacy-ui`), are both deprecated but not yet deleted — do not build new frontend work there.
8. **Hardware Resource Governor (V2)**: Real-time telemetry monitoring NVIDIA RTX 4060 GPU utilization, VRAM, CPU, and RAM via `nvidia-ml-py` (PyNVML) and `psutil`. Queues or rejects incoming tasks when safety thresholds are breached.
9. **Deterministic $O(1)$ Safety Permissions**: Hardcoded zero-hallucination security table classifying actions into `LOW_RISK`, `CONFIRMATION_REQUIRED`, and `HIGH_RISK`. Potentially dangerous actions return cryptographic SHA-256 tokens (`act_<hash>`) requiring user confirmation before execution.
10. **MCP & Skills Subsystems**: Model Context Protocol JSON-RPC 2.0 stdio bridge and dynamic YAML frontmatter markdown skills (`skills/*.md`).

---

## 2. Directory Structure & Key Files

```
JARVIS/
├── run_jarvis.py                   # Master entrypoint / launcher (starts backend & desktop UI)
├── Jarvis.bat                      # Windows batch launcher
├── README.md                       # High-level overview & quickstart
├── PLAN.md                         # Milestone implementation roadmap
├── governor_watchlist.json         # Process names monitored by the Resource Governor
│
├── docs/                           # Exhaustive technical documentation
│   ├── ARCHITECTURE.md             # Subsystem deep dive
│   ├── API_REFERENCE.md            # REST API endpoints & request/response schemas
│   ├── USER_GUIDE.md               # User interaction, hotkeys, custom skills
│   ├── GLASS_RENDERING.md          # Liquid-glass refraction technique (WinUI/Win2D)
│   └── AI_AGENT_CONTEXT.md         # This AI Agent Onboarding Guide
│
├── backend/                        # FastAPI Backend Application
│   ├── alembic/                    # Alembic schema migrations
│   │   └── versions/               # Version scripts (001_initial, 002_project_ext, 003_attachments)
│   ├── app/
│   │   ├── main.py                 # FastAPI application & router mounting
│   │   ├── config.py               # Pydantic Settings & environment variables
│   │   ├── database/               # SQLModel engine, session factory & DB models
│   │   │   ├── models.py           # SQLModel table definitions (Project, Session, Attachment, Artifact, etc.)
│   │   │   └── session.py          # SessionLocal factory & get_session dependency
│   │   ├── routers/                # Modular FastAPI APIRouters
│   │   │   ├── projects.py         # Project workspace CRUD & directory generator
│   │   │   └── artifacts.py        # Artifacts, version history & secure file upload
│   │   ├── agent/
│   │   │   ├── orchestrator.py     # Multi-turn agent loop, attachment injection & execution engine
│   │   │   ├── runtime_process_manager.py # Native llama-server.exe manager with --no-mmap
│   │   │   ├── model_provider.py   # Model provider interface & LlamaCppProvider
│   │   │   ├── model_router.py     # Local vs Heavy model router
│   │   │   ├── permissions.py      # Deterministic O(1) safety permission engine
│   │   │   ├── openrouter_client.py# OpenRouter cloud client for Heavy Mode
│   │   │   ├── validator.py        # Tool argument signature introspection
│   │   │   └── tools/              # Built-in native tools (read_file, write_file, patch_file, etc.)
│   │   ├── governor/
│   │   │   ├── resource_governor.py # PyNVML & psutil telemetry + adaptive queue
│   │   │   └── process_watcher.py  # Background process monitor & throttle
│   │   ├── memory/
│   │   │   ├── store.py            # SQLModel-backed MemoryStore (sessions & messages)
│   │   │   └── compactor.py        # Progressive context compaction
│   │   ├── mcp/
│   │   │   ├── client.py           # JSON-RPC 2.0 stdio client
│   │   │   └── manager.py          # MCP server lifecycle & tool bridge
│   │   ├── skills/
│   │   │   └── loader.py           # Dynamic YAML frontmatter markdown loader
│   │   └── voice/
│   │       ├── wake_word.py        # Keyword spotter ("Jarvis", "Hey Jarvis")
│   │       └── synthesizer.py      # TTS text cleaner & sanitizer
│   ├── requirements.txt            # Python dependencies
│   ├── .env.example                # Configuration template
│   └── tests/                      # Pytest automated test suite (206+ tests)
│
├── desktop-winui/                  # ACTIVE native desktop client (WinUI 3 / C#)
│   ├── Jarvis.slnx                 # Solution file
│   ├── Jarvis.Core/                # DTOs, JarvisApiClient (HTTP), SSE stream readers
│   ├── Jarvis.Glass/               # Liquid-glass rendering (GlassPanel, LiquidGlassCanvas,
│   │                                #   wallpaper/live-capture backdrop sourcing)
│   └── Jarvis.App/                 # Shell: MainWindow, HudWindow, view models, tray/hotkey
│                                    #   services, BackendHost (spawns the FastAPI backend)
│
├── desktop-app/                    # DEPRECATED (Next.js 14 + React + Tailwind + Tauri)
│   ├── src/
│   │   ├── app/                    # Next.js app router & main layout
│   │   ├── components/             # React UI components
│   │   │   ├── Sidebar.tsx         # Workspace / Project switcher & session history
│   │   │   ├── RightPanel.tsx      # 4-Tab Panel (Artifacts | Files | Context | Activity)
│   │   │   ├── Composer.tsx        # Message composer with paperclip file attachment
│   │   │   ├── ChatView.tsx        # Conversation viewport
│   │   │   └── GovernorPill.tsx    # Live hardware governor badge
│   │   ├── hooks/                  # React hooks (useChat, useGovernor)
│   │   └── lib/                    # API client, SSE streaming client & TypeScript types
│   ├── package.json
│   └── out/                        # Static export served by FastAPI backend at /ui
│
├── desktop/                        # DEPRECATED legacy UI (pywebview fallback)
│   └── DEPRECATED.md               # Deprecation documentation
│
└── workspace/                      # Active project workspace directories & files
```

---

## 3. Subsystem Architecture & Execution Flow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           DESKTOP CLIENT                                    │
│   • Native WinUI 3 shell (desktop-winui/) — Sidebar | RightPanel | Composer │
│   • Same SSE seam as the deprecated Next.js/pywebview UIs                   │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │ SSE Streaming (POST /chat/stream)
┌──────────────────────────────────────▼──────────────────────────────────────┐
│                            FASTAPI BACKEND SERVICE                          │
│                                                                             │
│  ┌─────────────────────────┐  ┌────────────────────────┐  ┌──────────────┐  │
│  │    Resource Governor    │  │   Safety Permissions   │  │ Model Router │  │
│  │ (GPU/VRAM/CPU Telemetry)│  │ (Deterministic O(1) Gating) (Normal/Heavy) │  │
│  └────────────┬────────────┘  └───────────┬────────────┘  └──────┬───────┘  │
│               │                           │                      │          │
│  ┌────────────▼───────────────────────────▼──────────────────────▼───────┐  │
│  │                           Agent Orchestrator                          │  │
│  │   • Multi-Turn Tool Loop                 • Context Compaction         │  │
│  │   • Attachment Context Injection         • MCP Stdio Client Bridge    │  │
│  │   • Dynamic Skills Loader                • Native llama.cpp Provider  │  │
│  └────────────┬──────────────────────────────────────────────────┬───────┘  │
│               │                                                  │          │
│  ┌────────────▼────────────┐                       ┌─────────────▼───────┐  │
│  │    SQLModel Database    │                       │  llama-server.exe   │  │
│  │ (Projects, Sessions,    │                       │  (Qwen3.5-9B / 4B)  │  │
│  │  Attachments, Artifacts)│                       │  -ngl 99 --no-mmap  │  │
│  └─────────────────────────┘                       └─────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 4. Key Developer Commands

- **Run Full App**: `python run_jarvis.py` (or `.\Jarvis.bat`)
- **Run Full App with Legacy UI Fallback**: `python run_jarvis.py --legacy-ui`
- **Run Backend Standalone**:
  ```powershell
  cd d:\JARVIS\backend
  ..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
  ```
- **Build & Run the WinUI Client** (active frontend):
  ```powershell
  cd d:\JARVIS\desktop-winui
  dotnet build Jarvis.slnx
  dotnet run -c Debug --no-build --project Jarvis.App
  ```
  Kill any running `Jarvis.App` process first — a live instance locks the AppX output and
  `dotnet run` fails with an unhelpful `MSB3027` rather than a clear "already running" error.
- **Run Frontend Dev Server** (deprecated Next.js UI):
  ```powershell
  cd d:\JARVIS\desktop-app
  npm run dev
  ```
- **Build Frontend Static Export** (deprecated Next.js UI):
  ```powershell
  cd d:\JARVIS\desktop-app
  npm run build
  ```
- **Run Backend Pytest Suite**:
  ```powershell
  cd d:\JARVIS\backend
  ..\.venv\Scripts\python.exe -m pytest -o pythonpath=". .." tests/
  ```
