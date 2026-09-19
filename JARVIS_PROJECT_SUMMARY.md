# Project Jarvis (Nexus) — Executive Project Summary

## System Overview
- **Primary Runtime Engine**: Native `llama-server.exe` (llama.cpp) on `http://127.0.0.1:8001`
- **Active Models**:
  - Main: `Qwen3.5-9B Q4_K_M`
  - Fast: `Qwen3.5-4B Q4_K_M`
- **Hardware Optimization**: 100% GPU offloading (`-ngl 99`) and system RAM conservation (`--no-mmap`) tailored for NVIDIA RTX 4060 (8GB VRAM) and 16GB DDR5 system RAM.
- **Backend Architecture**: FastAPI on `http://127.0.0.1:8000` with Server-Sent Events (`/chat/stream`) token streaming.
- **Database & Persistence**: Unified SQLModel ORM + Alembic schema migrations on startup (`data/jarvis_memory.db`).
- **Desktop Frontend**: native WinUI 3 / C# client in [`desktop-winui/`](desktop-winui/) (`Jarvis.App` shell, `Jarvis.Core` HTTP/SSE client, `Jarvis.Glass` liquid-glass renderer) with workspace switching, a right panel (`Artifacts`, `Files`, `Context`, `Activity`, `Reasoning`), image attachments, tray icon and an always-on-top HUD. It is the only client and supervises the backend process itself.
- **Safety & Governance**: PyNVML Hardware Resource Governor (V2) with deterministic $O(1)$ SHA-256 action confirmation tokens.
- **JARVIS Behaviour Layer**: Persona profiles (manner without changing capability), a hands-free wake-word voice loop with barge-in, ambient hardware awareness that speaks up on its own, and an always-on-top HUD overlay on `Ctrl+Shift+J`.
- **Current Phase**: Context Engine & RAG complete; JARVIS behaviour layer complete and verified; WinUI migration complete — the Next.js/Tauri and pywebview clients were removed (September 2026).
