# Jarvis — Private Local AI Assistant for Windows

A private, fast, and lightweight local AI assistant for Windows 11 powered by native `llama-server.exe` (llama.cpp) running **Qwen3.5-9B** and **Qwen3.5-4B** with full GPU offload (`-ngl 99 --no-mmap`), a native WinUI 3 desktop client with liquid-glass rendering, workspace switching, attachments, artifacts versioning, Model Context Protocol (MCP), dynamic skills loading, hardware resource governance, and voice interaction.

---

## Key Features

- **Native llama.cpp Execution**: Primary runtime uses `llama-server.exe` on port 8001 with 100% GPU VRAM offload and `-no-mmap` to conserve system RAM.
- **Native WinUI 3 Desktop Client**: `desktop-winui/` — a C# shell (`Jarvis.App`), typed HTTP/SSE client (`Jarvis.Core`) and a D3D11 liquid-glass renderer (`Jarvis.Glass`) with Project Workspaces, a right panel (Artifacts, Files, Context, Activity, Reasoning), image attachments and a tray icon. Talks to the backend purely over HTTP + SSE.
- **SSE Streaming & FastAPI Backend**: Real-time token streaming via `/chat/stream` with tool execution updates and final metadata.
- **Unified SQLModel & Alembic Database**: Robust relational schema managing Projects, Sessions, Attachments, Artifacts, and Version History with automatic startup migrations.
- **Hardware Resource Governor (V2)**: Real-time PyNVML GPU/VRAM telemetry protecting your RTX 4060 GPU and CPU from overload.
- **Deterministic Safety Permissions**: $O(1)$ hardcoded security tiering (`LOW_RISK`, `CONFIRMATION_REQUIRED`, `HIGH_RISK`) with SHA256 action approval tokens.
- **Model Routing (Normal vs Heavy)**: Local inference by default, dynamically routing complex reasoning to OpenRouter when enabled.
- **Model Context Protocol (MCP)**: Bidirectional JSON-RPC 2.0 stdio client for external tools and servers.
- **Dynamic Skills Loader**: Extensible Markdown-based domain skills (`skills/*.md`) with trigger keyword matching.
- **Hands-Free Voice Loop**: Wake-word detection ("Jarvis" / "Hey Jarvis") with local voice-activity detection, a 15-second follow-up window so follow-ups need no wake word, barge-in to interrupt a spoken reply, and neural TTS.
- **Persona Layer**: Selectable manner (`jarvis`, `assistant`, `operator`) controlling address, voice, tone and spoken reply length - without ever altering the tool protocol.
- **Ambient Awareness**: Proactive hardware observations (VRAM, thermals, RAM, disk, battery, model eviction) streamed over SSE, announced once rather than repeatedly, plus instant telemetry-assembled status briefings.
- **Always-On-Top HUD**: A borderless glass overlay summoned anywhere with `Ctrl+Shift+J` - voice orb, live telemetry rings, and its own voice session.

---

## Quick Start

### 1. Launch Jarvis
Double-click `Start Jarvis.bat` (or run it from a shell):
```powershell
.\"Start Jarvis.bat"
```
It builds the WinUI client on first run, registers it as a development package, and launches it.
The app starts the backend itself (`.venv\Scripts\python.exe -m uvicorn ...`) inside a Job Object,
so nothing is left running when the window closes. Pass `-Build` to force a rebuild.

### 2. Run in Development Mode
**Backend:**
```powershell
cd backend
..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

**Frontend (WinUI):**
```powershell
cd desktop-winui
dotnet build Jarvis.slnx
dotnet run -c Debug --no-build --project Jarvis.App
```
Kill any running `Jarvis.App` first — a live instance locks the build output.

### 3. Run Automated Tests
```powershell
.\.venv\Scripts\python.exe -m pytest
```
`pytest.ini` sets the path, so this works from the repository root.

---

## Documentation

Detailed technical documentation is available in the [`docs/`](docs/) directory:
- [**AI_AGENT_CONTEXT.md**](docs/AI_AGENT_CONTEXT.md): Master onboarding guide for AI coding assistants.
- [**ARCHITECTURE.md**](docs/ARCHITECTURE.md): Architectural deep dive across backend subsystems.
- [**API_REFERENCE.md**](docs/API_REFERENCE.md): REST & SSE API endpoint reference.
- [**USER_GUIDE.md**](docs/USER_GUIDE.md): Workspaces, keyboard shortcuts, custom skills, and configuration.
- [**PLAN.md**](PLAN.md): Implementation milestones and build plan.
