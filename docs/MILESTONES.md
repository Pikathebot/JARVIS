# Jarvis -- Milestone Log

What has been built, in the order and words it was recorded when each piece landed: the reason
for each change, what was measured, and what pins it in the tests. Moved out of `PLAN.md` on
2026-09-23 so the plan can stay short; nothing here was edited in the move. New entries go at the
end of section 3 as milestones leave `PLAN.md` section 4.

Note: entries describe the system *as of when they were written*. Later entries supersede earlier
ones (e.g. the SYSTEM STATE prompt line was removed on 2026-09-22; the Next.js client was deleted
on 2026-09-19; the fast slot is MiniCPM5-2B, not the 4B, since 2026-09-21). `git log` holds the
full detail of each commit.

## 2. Implemented Milestones (Status: Complete)

- [x] **Phase 0 — FastAPI Core & SSE Streaming**: Real-time `/chat/stream` SSE generator and `/chat` endpoints.
- [x] **Phase 1 — Native Tool Calling**: Inspect-based signature introspection, tool registration, and parameter validation.
- [x] **Phase 2 — Deterministic Safety Permissions**: $O(1)$ hardcoded security tiering (`LOW_RISK`, `CONFIRMATION_REQUIRED`, `HIGH_RISK`) with SHA256 action tokens.
- [x] **Phase 3 — Hardware Resource Governor (V2)**: PyNVML GPU/VRAM telemetry + `psutil` CPU/RAM monitoring with automatic load throttling.
- [x] **Phase 4 — Model Router & Runtime Process Manager**: Local llama.cpp process manager with `-ngl 99 --no-mmap` execution flags.
- [x] **Phase 5 — Database Unification (SQLModel + Alembic)**: Unified schema across projects, sessions, messages, attachments, and artifacts.
- [x] **Phase 6 — Project Workspaces & Section 7 Filesystem**: Directory hierarchy under `workspace/projects/{project_id}/` (`files/`, `knowledge/`, `artifacts/`, `memory/`, `indexes/`).
- [x] **Phase 7 — Artifacts, Attachments & Context Injection**: Versioned artifact storage, secure attachment uploads, and automatic LLM context turn injection.
- [x] **Phase 8 — Next.js Desktop Application** (superseded, deleted 2026-09-19): `desktop-app/` was the canonical client until the WinUI 3 migration. Once the WinUI client covered every backend surface, it, the pywebview `desktop/` fallback, `run_jarvis.py`/`Jarvis.bat` and the `/ui` static mount in `main.py` were removed; `Start Jarvis.bat` → `scripts/start-jarvis.ps1` is the launcher and `BackendHost` starts uvicorn.
- [x] **Phase 10 — WinUI 3 native client + liquid glass (2026-09-14)**: `desktop-winui/` replaces the web client. `Jarvis.Glass/Liquid/` puts a D3D11 glass `SwapChainPanel` under any WinUI window's content (`GlassHost`), renders per-layer displacement → refraction → specular-rim passes over a live `Windows.Graphics.Capture` of the desktop, and ships glass controls (`GlassToggle`, `GlassSlider`, `GlassButton`, `GlassSegmented`, `GlassTextField`) plus DirectWrite text on the glass layers. Settings is an inline glass pane; controls nested in a slab/ScrollViewer clip and layer correctly and stay in step with scrolling (`GlassScroll`). Slabs cast shadows; the glass moves with the window in the same DWM frame (SwapChainPanel, 2026-09-15); rendering is change-driven with per-layer output caching, scissored control layers and reduced-res frost (~6% GPU idle under a changing desktop). Message bubbles, session rows and the workspace menu (a floating glass card with a vertical `GlassSegmented` puck) are glass too (2026-09-15).
- [x] **Phase 9 — Context Engine & RAG**: Syntax-aware chunking, local CPU embeddings, hybrid keyword + vector retrieval, and per-project workspace scoping for tools and permissions.

---

## 3. The JARVIS Behaviour Layer

Everything above makes Jarvis *capable*. This layer is what makes it behave like Jarvis rather than a chat window with tools.

- [x] **Persona** (`backend/app/persona/`) — Three profiles (`jarvis`, `assistant`, `operator`), each with an address term, voice, tone directives and a spoken-length cap. The system prompt is composed as *persona preamble + invariant tool protocol*: a persona changes manner, never capability. Active persona and user overrides persist to `data/persona.json`.

- [x] **Hands-free voice** (`backend/app/voice/session.py`, `desktop-winui/Jarvis.App/ViewModels/VoiceViewModel.cs`) — The client owns the microphone and does local voice-activity detection, so only whole utterances are uploaded; the backend owns what an utterance *means*. A wake word dispatches immediately; a bare "Jarvis" arms the session and speaks the greeting; for 15 seconds after a reply, follow-ups need no wake word. Talking over a spoken reply cuts it off.

- [x] **Ambient awareness** (`backend/app/awareness/`) — Pure rules over a hardware snapshot (VRAM, thermals, RAM, CPU, disk, battery, model eviction, heavy external apps), with the monitor owning all restraint: announce once, escalate through the cooldown, stay quiet on de-escalation, restate at most every 5 minutes, and announce recovery exactly once. Observations stream over SSE and are spoken only while hands-free voice is on. Briefings are assembled from telemetry rather than generated, so they are instant and their numbers are always real.

- [x] **HUD overlay** (`desktop-winui/Jarvis.App/HudWindow.xaml.cs`) — A transparent, always-on-top window summoned from anywhere with `Ctrl+Shift+J`. Voice orb, two telemetry rings, and the last thing said in either direction. It runs its own voice session so an ambient question does not interleave with the main window's work.

- [x] **Scheduled routines** (`backend/app/routines/`) — Time-of-day briefings and custom messages ("every weekday at 8am, give me the status briefing"), persisted to `data/routines.json`. The scheduler polls the wall clock and fires through the awareness monitor's own `emit()` channel, so a routine is delivered, spoken, and shown in the tray exactly like any other observation — no separate frontend plumbing needed. Managed from Settings → Scheduled Routines (`SettingsDialog.tsx`); `POST /api/routines/{id}/run` previews one immediately.

- [x] **Proactive tool use** (`AwarenessMonitor.actions`, `backend/app/main.py`) — A kind of observation can carry a registered action: when it first escalates to CRITICAL, the monitor awaits the action, then emits a follow-up observation announcing what it did. Ships with one wired case — VRAM critical now evicts the model itself (`auto_unload_models()`) rather than only warning that eviction is imminent, which fires independently of and earlier than the governor's own reactive throttle (which additionally needs high GPU compute or ≥99% raw VRAM). An action fires once per escalation, never every poll, and re-arms after recovery. Toggle: Settings → "Proactive actions" (`PATCH /api/awareness/config {actions_enabled}`), default on (`PROACTIVE_ACTIONS_ENABLED`).

- [x] **Voice-driven confirmations** (`build_confirmation_prompt` in `backend/app/agent/permissions.py`; client-side yes/no interception is `ConfirmationIntentParser` in `desktop-winui/Jarvis.Core/Voice/`, see the 2026-09-19 entry below) — A `CONFIRMATION_REQUIRED` tool call now carries a TTS-ready `spoken` prompt ("I need your approval to run a command: git push origin main. Say yes to proceed, or no to cancel, sir.") alongside the existing approve/deny card, on both `/chat` and `/chat/stream`. The main window speaks it when the turn was voice-initiated; the HUD speaks it always, since every HUD turn is voice. A spoken "yes"/"no" (`parseConfirmationIntent`) is intercepted before it reaches the model — "yes" resubmits the original prompt with all pending action ids approved, "no" cancels, anything else re-asks rather than being treated as a new command.
- [x] **Confirmation timeouts** (`backend/app/agent/confirmations.py`, `POST /api/confirmations/{id}/deny`) — `CONFIRMATION_TIMEOUT_ACTION=deny` used to be a constant with nothing behind it: an unanswered ask just went quiet, and an `act_<hash>` token never expired, so a "yes" minutes later still ran the action. Every ask is now registered with a deadline (`CONFIRMATION_TIMEOUT_SECONDS`, default 90; 0 = never). A watcher sweeps the registry and announces lapsed asks through `AwarenessMonitor.emit()` as a `confirmation_timeout` warning — "I did not hear back about whether to execute command: git push, so I have not done it, sir. Ask again if you still want it." — so it is spoken and shown like any other unprompted remark, one sentence per session however many actions lapsed. A lapsed token arriving in `approved_action_ids` is refused: the action is re-evaluated, comes back as `confirmation_required`, and the prompt says the earlier approval timed out. Tokens the running process never issued (a restart) pass through to the hash check as before. Denying in the WinUI client now tells the backend (`ChatViewModel.DenyAction` → `DenyConfirmationAsync`), so a declined action is dropped quietly instead of being announced as a timeout later. Verified live end-to-end with a 20-second window.
- [x] **Proactive follow-ups** (`backend/app/awareness/actions.py`) — Two more things Jarvis does about an observation rather than just naming it, both read-only. A heavy external app (`heavy_external_app`) that stays open past `Thresholds.heavy_app_nudge_seconds` (10 min) escalates from the initial notice to a spoken WARNING with the numbers that matter: "Blender has held 3.9 gigabytes of VRAM and 1.3 gigabytes of RAM for 11 minutes; the GPU has only 0.6 gigabytes free, so my model has little headroom. Closing it would give that back, sir." The monitor keeps a first-seen clock per app; per-process VRAM comes from the Windows `GPU Process Memory` counter via `win32pdh` (on WDDM, NVML reports every process as N/A), with readings above the card's total discarded — the NVIDIA overlay's counter is known to claim ~90 GB on an 8 GB card. Jarvis never closes the app itself. When `disk_space` goes CRITICAL the `disk_space` action scans the project workspace and `models/` for the five largest files and speaks them ("The largest things I manage are models/qwen3.5-9b/… at 4.7 gigabytes, …") — instant on this tree, capped at 200k entries so it can never pin the CPU.

- [x] **Model selection** (`backend/app/agent/model_catalog.py`, `backend/app/routers/models.py`) — the two model slots used to be pinned by `LLAMA_MAIN_MODEL_PATH` / `LLAMA_FAST_MODEL_PATH` and could only be changed by editing `.env` and restarting; there was no endpoint for it at all. The catalogue enumerates every GGUF under `models/`, remembers a per-slot choice in `data/models.json`, and is consulted by `RuntimeProcessManager` ahead of the configured paths, so a choice survives restarts without anyone hand-editing configuration. `main` (what chat runs on) and `fast` (the router's smaller fallback) are chosen independently. Models at most one folder deep under `models/` (`models/qwen3.5-9b/…`) are marked `recommended` and returned first — that is where deliberately installed models live, one per folder, whereas deeper trees are vendor download mirrors (`models/unsloth/<repo>/…`) holding duplicates and companion files. `mmproj-*.gguf` projectors are reported separately and cannot be selected as chat models; each model is paired (`projector`) with the projector sitting in its own folder, which is the one signal on disk saying which model it was converted from. Selecting activates by default, restarting llama-server onto the model — including sweeping a server that was started outside Jarvis, which otherwise keeps serving the old weights and makes the switch a silent no-op. A model that will not load keeps its selection and returns the reason rather than discarding the choice. Managed from Settings → Models.
- [x] **Vision** (`backend/app/agent/runtime_process_manager.py`, `backend/app/memory/context_manager.py`) — a model launched with an `mmproj-*.gguf` beside it gets `--mmproj` automatically, so image attachments are seen rather than just stored. The orchestrator asks `RuntimeProcessManager.has_vision(slot)` per turn and, when the serving model has a projector loaded, `ContextManager` sends PNG/JPEG/WebP/GIF/BMP attachments (≤4 per turn, ≤10 MB each) as `image_url` data-URI parts on the user message; a text-only model, or a cloud turn, gets the file-path note as before. The projector is the one optional part of the launch: on an 8 GB card it needs its own ~0.7–0.9 GB, so a launch that fails with it is retried text-only rather than leaving no model at all, and `has_vision` then reports what actually loaded. `LLAMA_MMPROJ_ENABLED=false` keeps things text-only; `LLAMA_MMPROJ_OFFLOAD=false` keeps the projector on the CPU. Verified live on both slots: the 4B + F16 projector sits at ~6.4 GB, the 9B + F16 projector at ~7.6 GB of 8.2 — it fits, but with little headroom for other GPU-holding apps. Stale `.env` model paths (the per-folder move) fall back through the catalogue by family.
- [x] **Image captioner sidecar** (`backend/app/agent/captioner.py`, `models/captioner/`) — A projector only helps the model it was trained for, so anything else (a text-only GGUF on `fast`, a Heavy Mode turn, a launch that dropped its projector) saw images as file paths. A second `llama-server` on port 8002 runs SmolVLM-500M-Instruct with its own `mmproj`, CPU-only (`-n-gpu-layers 0`, `--no-mmproj-offload`), and turns each image attachment into a description that goes on the turn as attachment text ("Image contents (as seen by a vision model; treat this as what the picture shows): …"). The orchestrator only calls it when `has_vision()` is false for the serving slot, so a model with a projector still gets the real pixels. Always cold: spawned on the first image (~5 s), ~0.6 s per caption after that, stopped after `CAPTIONER_IDLE_SECONDS` (300) idle, zero VRAM. The captioner folder is hidden from the chat-model catalogue (`SIDECAR_DIRS`), its pid is in `PROTECTED_PIDS` so the emergency llama-server sweep leaves it alone, and it reaps stale copies of itself before starting. A 500M model's captions are accurate but terse (subject and text, not background colours); `CAPTIONER_MODEL_PATH` swaps in a bigger one. Status on `GET /api/models` under `captioner`, next to the new `loaded_projector`.
- [x] **Children die with the backend** (`backend/app/agent/process_guard.py`) — Both llama-server children are assigned to a Windows Job Object with `KILL_ON_JOB_CLOSE`, so a crash or `Stop-Process -Force` no longer leaves the old weights serving on :8001 for the next backend to adopt. Found because a 10:43 Qwen-4B server survived every restart and select all morning: on Windows a second `llama-server` binds an occupied port *without error* and sits unreachable behind the first, so `select` reported success while the old model kept answering. `RuntimeProcessManager.reload()` now also probes the port for a foreign server instead of trusting a flag it only learns from a health check.
- [x] **Tool execution pipeline** (`backend/app/agent/tool_pipeline.py`) — Borrowed from the shape of DeepSeek Harness's `tools/pre-execute → execute → post-execute → tool/result` waterfall. Native tools were synchronous functions called straight from the async loop, so a hung command blocked the whole backend (SSE, awareness, voice), and results reached the model verbatim however large. Now every tool, native or MCP, runs through one path: native tools on a worker thread, MCP tools awaited, both under a per-tool timeout (`TOOL_TIMEOUTS` per tool, `TOOL_DEFAULT_TIMEOUT_SECONDS` otherwise; `execute_command` 180 s, `unreal.build` 15 min). A timeout returns an error the model can read and the turn moves on — the thread cannot be killed, so the tool may finish in the background, and the model is told not to repeat the call. Ordered post-execute hooks may replace or block a result before the model sees it; the built-in one caps output at `TOOL_RESULT_MAX_CHARS` keeping head and tail. `tool_end` events and `tools_used` now carry `duration_ms`, `timed_out`, `truncated`, and a `timeout` status distinct from `error`. Verified live: a 30-second ping under a 3-second limit came back as `timeout` at 3.0 s with the loop still serving.
- [x] **Image offload on overflow** (`backend/app/agent/image_offload.py`) — Image tokens are decided by the projector at encode time, so the context manager's per-image reservation is a guess; when it is low, llama-server rejects the whole request. `LlamaCppProvider` now recognises that rejection on both `chat` and `stream_chat`, replaces every `image_url` part with a placeholder, and retries once — the model answers without the pictures and says why, instead of the turn failing.
- [x] **Real numbers in the prompt, real apps from the launcher** — Two things found by asking the 4B ordinary things. "hi" produced *"Temperatures nominal, VRAM at 2.4GB free"*: the persona told the model to "refer to system state in concrete numbers" and nothing gave it any, so it invented them. `build_system_prompt` now carries a SYSTEM STATE line from the awareness monitor's latest sample (GPU/VRAM/temp/RAM/CPU/disk/battery/loaded model, ≤20 s old) with the rule that those are the only hardware figures it may quote and that they belong in answers about the machine, not greetings; the persona directive says the same. Verified: "how much vram is free and how hot is the gpu" → 6.3/8.0 GB, 1.7 GB free, 53 °C, identical to `/api/awareness/status` at that moment; "hi" → "Good day, sir."; CPU temperature (not sampled) → "has not been checked". "open settings" / "open discord" did nothing because `resolve_executable` returned the bare word for anything not on PATH and `launch_app` always demanded confirmation. `app_control` now resolves Windows URI apps (`ms-settings:`, `ms-windows-store:`, …), then PATH, then Start Menu shortcuts (exact → prefix → contains, uninstallers skipped), and opens shortcuts/URIs via the shell; unknown names return a clear error instead of a failed `Popen`. `launch_app` is argument-aware in `permissions.py`: an installed app by name is LOW_RISK, an explicit path, script, or anything under Downloads/Temp still asks. Verified live: both opened without a card.

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

- [x] **Typed and spoken yes/no for confirmations in the WinUI client (2026-09-19)** (`desktop-winui/Jarvis.Core/Voice/ConfirmationIntent.cs`, `ChatViewModel`, `HudWindow`) — `ChatViewModel.SendMessageAsync` used to attach *every* pending `act_<hash>` id to whatever the next message was, so "no", an unrelated question or a stray voice utterance all approved the action. While an ask is pending, the composer and the HUD now read the reply through `ConfirmationIntentParser` first: a clear yes ("yes", "go ahead", "do it", a trailing "ok") resubmits the prompt that led to the ask with every pending id attached — the same thing the approve button does, since `/chat/stream` rejects an empty message — and the user's own words stay in the transcript; a no ("no", "cancel", "don't", "stop") tells the backend via `POST /api/confirmations/{id}/deny` so no timeout is announced later, and the voice loop hears "Understood, cancelled." (`ChatViewModel.SpokenFeedback`); anything else goes to the model with nothing approved and the card stays until it is answered or the backend's window lapses. A negation up front wins when both appear ("don't do it"), "yes… actually no" is treated as unclear, and "ok"/"sure"/"fine" only count as the whole answer or its last word so "ok so what does that do?" is a question. Resolving by text or voice takes the card off its bubble (`ChatMessage.ResolveConfirmation`) and clicking a card that a typed "yes" already resolved is a no-op, so an id is never sent twice. `confirmation_required` is also treated as the turn's completion on the client — the backend ends the stream there without a `done` — so the caret goes away and the spoken prompt is actually spoken. `scripts/start-jarvis.ps1` now runs the winapp CLI's `RunPackagedApp` target with `WinAppRunNoLaunch=true` after a build, because a plain `dotnet build` leaves the AppX layout stale and the launcher would run the previous build.
- [x] **Vision in the WinUI client (2026-09-16)** (`desktop-winui/Jarvis.App/Services/ImageAttachmentService.cs`) — thumbnail chips for image attachments, Ctrl+V pastes a clipboard bitmap as an attachment, anything over 1568 px on its longest side is downscaled to a JPEG before upload (Qwen3.5 uses dynamic resolution, so a 4K capture would cost thousands of tokens), and a hint line under the composer says whether the loaded model will see the image itself (`loaded_projector` on `GET /api/models`) or the captioner will describe it. Uploading into a brand-new session used to fail its foreign key because the client uploads before the first message — the session row is now created on upload.

- [x] **Honest tool turns (2026-09-20)** (`backend/app/agent/orchestrator.py`, `persona/profiles.py`, `llamacpp_provider.py`) — "delete probe_delete_me.txt" came back *"Sir, the file has been deleted."* with `tools_used: []`. Replaying the exact request against llama-server (new `LLAMA_PAYLOAD_DUMP_DIR` writes every request as JSON) found three things stacked up. The prompt shipped twice: `ContextManager.build_context` already puts its assembled system prompt at the head of `messages`, and every provider loop prepended the same text again — ~10 KB of persona, rules and RAG context in duplicate on every local turn since the first commit (`_with_system_prompt` now sends exactly one). The Jarvis persona line *"Never narrate what you are about to do — do it, then report what happened"* was the actual trigger: with it the 4B called the tool 1 time in 6, without it 6 in 6, so it now says to call the tool and report only from its result. And the router only sees prose, so a short command landed on the fast slot; tools are now chosen before routing and a turn whose tool set includes anything above LOW_RISK (`delete_file`, `execute_command`, `launch_app`, clipboard) goes to main (`_prefer_capable_slot`, explicit model choice respected). The 9B then showed the other failure — think block *"Found it. Now I'll delete the file."* and stop — which the provider used to promote to the reply; the stream loop now nudges once ("call the tool now, or give your answer") with the tools still offered, and only then falls back to prose. Each loop logs the tools offered and what the model returned (`Offering 9 tools to 'main': [...]` / `Model 'main' replied: tool_calls=['delete_file']`). Verified live: auto route → main → `find_files` → `delete_file` → confirmation card → approve → "Done. The file is gone."; the 4B forced by `model=fast` now asks for confirmation too. Pinned by `backend/tests/test_orchestrator_tool_turns.py`.

- [x] **Hardware on demand, the clock on every turn (2026-09-22)** (`backend/app/agent/tools/system_status.py`, `build_turn_context`) — The SYSTEM STATE line gave the model real numbers, but it also put the machine's vitals in front of every turn and the persona's "concrete numbers" directive pulled them into replies that never asked. The line is gone; a question about the machine ("how hot is the GPU", "what's my system usage", "how's my pc" — word-boundary matched, so "program" does not trip "ram") now offers `get_system_status`, a LOW_RISK tool that takes a fresh sample through `AwarenessMonitor.collect_snapshot()` rather than the ≤20 s-old poll, and returns the same one-line summary plus governor state and throttle reasons. The persona says to call it and quote only what it returned, never to volunteer figures. In its place the per-turn block carries `CURRENT TIME: Tuesday 2026-09-22 14:07 India Standard Time` (minute resolution, so the several requests inside one turn share a prefix) — the model had no idea what "today" was and phrased current-affairs searches around its training year. Still placed after the history, never in the cached head. Pinned by `backend/tests/test_system_status_tool.py`.

- [x] **New workspace from the WinUI client (2026-09-22)** (`desktop-winui/Jarvis.App/Views/NewWorkspacePane.xaml`) — `ProjectsViewModel.CreateAsync` and `POST /api/projects` both worked, but nothing in the client called them: the workspace dropdown only listed and switched projects. A "New workspace" button under the menu's rows (a button, not a row — the puck should not land on an action) opens an in-window glass sheet shaped like `SettingsPane`: name, description, standing instructions, and local folders to index (FolderPicker). Creating also activates the workspace, since the backend only activates the very first project on its own.
- [x] **Our own compute is not an external workload (2026-09-22)** (`RuntimeProcessManager.is_processing`, `ResourceGovernor.runtime_busy_provider`) — Only requests that come through the orchestrator register a governor activity; anything sent straight to `:8001` (a payload replay with curl, a script, a second client) drove the GPU to 98% with nothing registered, which the governor read as a game and — after the 4-tick debounce — evicted the model. Each poll now asks llama-server first (`GET /slots`, `is_processing` per slot, 0.5 s timeout, awaited in the poll loop so `collect_metrics` stays synchronous) and, when it is decoding, the GPU-compute breach is not counted. Only the compute rule is exempted: VRAM growth from *other* processes is still judged as before, since that is the signal that actually says something else wants the card. Unknown (no server, endpoint unreachable, adopted server without `/slots`) is treated as not busy, so the compute rule keeps protecting against a real external load. Verified against the live server: `is_processing` False idle, True mid-request, False after. Pinned in `backend/tests/test_governor.py`.

- [x] **RAG relevance floor (2026-09-22)** (`backend/app/rag/embedding_sidecar.py`, `embeddings.py`, `retriever.py`, `vector_store.py`) — Workspace context went into every WORKSPACE turn because something always ranks first and nothing could gate it: `sentence_transformers` was never installed, so "semantic" vectors were a hashed bag of words with no signal, and the lexical reranker's scores meant nothing. Four changes. (1) Real vectors: a CPU-only llama-server embedding sidecar (nomic-embed-text-v1.5 Q8, 137M, 768-dim, `models/embeddings/`, port 8003) in the captioner's shape — cold, spawned on the first vector, stopped after 300 s idle, zero VRAM; ~35 ms per query, `backend/app` (497 chunks) indexes in 20 s. Without the model file it degrades to the hashed engine. (2) An embedding-generation marker beside each project's index (`indexes/embedding_model.txt`): an index built in another vector space is wiped and rebuilt rather than compared against — the indexer does it on the next pass, the retriever kicks a background rebuild off once and serves nothing until then. (3) Every candidate gets the same cosine scale (keyword-only hits read their stored vector back from Qdrant), then three cuts calibrated on this repo's own code, where a real question peaks at 0.71–0.97 and everything unrelated sits flat at 0.52–0.65: nothing below `RAG_MIN_RELEVANCE` (0.62), nothing more than `RAG_RELEVANCE_GAP` (0.15) under the best chunk, and nothing at all when the top six are within `RAG_MIN_PEAK` (0.04) of each other — "what is the weather", "write me a poem about cats" and "why?" all score 0.55–0.63 flat against the code and now inject zero chunks; "how does the wake word get detected" keeps exactly `WakeWordDetector` at 0.893. (4) `should_retrieve`: chit-chat, a message with no content terms ("why?"), and a short imperative aimed at the machine ("delete probe.txt", "open discord", "set volume to 50", ≤6 words starting with an OS/file verb and containing no question word) skip retrieval outright — those score 0.62–0.68 flat against `delete_*` handlers and would otherwise need the peak rule to catch them. `GET /api/models` reports the sidecar under `embedder`. Pinned by `backend/tests/test_rag_relevance_floor.py` (the sidecar test runs only where the GGUF is installed).

- [x] **Sidecars hold no VRAM (2026-09-22)** (`captioner.py`, `embedding_sidecar.py`) — `models/captioner/` had gone missing from disk, so the image captioner was `available: false`; re-downloaded SmolVLM-500M-Instruct Q8 + its projector from `ggml-org` and verified the fallback end-to-end with a genuinely text-only model: MiniCPM5-2B on `main` (`loaded_projector: null`) answered "a blue sign with a red square and a yellow circle, and the text reads 'Jarvis Test 42'" for exactly that image, 4.0 s including the sidecar's cold start. Found while checking: the CUDA build of llama-server creates a device context even with `-n-gpu-layers 0`, so each CPU sidecar held ~170 MB of dedicated VRAM it never used (181 MB embedder, 161 MB captioner via the `GPU Process Memory` counter) — and 178 MB was precisely the margin by which the 9B's projector had just failed to fit (5194 needed, 5016 free). Both sidecars now launch with `CUDA_VISIBLE_DEVICES=-1` (`""` is ignored by CUDA); ggml logs "no CUDA-capable device is detected", the counter reads 0 MB, and captions and embeddings are unchanged.

- [x] **Glass interaction and layering fixes (2026-09-22, verified on device)** — two bugs found from
  the user's report that the sheets blended into the window and that a toggle flipped when a drag
  ended on the side it started from. (1) `ReleasePointerCapture` raises `PointerCaptureLost`
  *synchronously* and that handler clears `_dragging`, so every release that read `_dragging`
  afterwards saw false: `GlassToggle` took the tap branch on every release (a drag that crossed
  sides looked correct by accident; one ending where it began flipped wrongly) and `GlassSegmented`
  always picked the row under the pointer rather than the dragged-to one. The drag state is now read
  before capture is released, in `Jarvis.Glass` and `Jarvis.GlassLab` alike. (2) Both sheets were
  declared `Layer="2"` in XAML — the same layer as every control inside every panel, and a layer
  only covers and only frosts what is *below* it, so a sheet could do neither to the controls behind
  it however high its `Frost` went. Sheets are now layer 4 via `GlassLayers`, which documents the
  whole census; no glass layer is a bare integer in XAML any more. XAML text still needs the
  separate `SheetBackdropOpacity` fade, because XAML always paints above the swapchain.

- [x] **Git tools, and the dead code around them deleted (2026-09-23)** (`backend/app/agent/tools/git.py`,
  `read_file.py`) — A read of the codebase found a second tool system that had never been reachable:
  `backend/app/tools/` held 21 `BaseTool`s (git, Unreal, terminal, patch, web, vision) registered in
  `main.py`, but only `AgentLoop` consumed that registry and the orchestrator constructed `AgentLoop`
  without ever running it -- the model had never been offered any of them. The useful parts were
  ported into the live system: `git_status`, `git_diff`, `git_log` (LOW_RISK) and `git_commit`,
  `git_checkout` (CONFIRMATION_REQUIRED; the spoken ask names the commit message or target), run as
  argv lists in the active workspace, never through a shell, so a path or message is one argument;
  offered on git words (``-bounded, so "different", "report", "log in" don't trigger). `read_file`
  gained `start_line`/`end_line`, labelled "[Lines a-b of N]", because a long file's result is cut to
  its head and tail and the middle was otherwise unreachable. The user chose not to port the Unreal
  tools. Then deleted: `app/tools/`, `agent/loop.py`, `tool_result_truncator.py`, the `sandbox/`,
  `vision/`, `search/` packages, `services/patch_validator.py` and `file_version_service.py` (only
  those tools used them), the Ollama and LM Studio fallback providers and the router's
  bonsai/hermes3/ollama runtimes, the reliability monitor (it only ever acted on a "bonsai" backend
  that no longer exists, and its rollback target was Ollama), and the backend-side Chatterbox speech
  with its `/voice/output` endpoints and "voice on/off" chat intercepts (the client speaks through
  `/api/voice/say` with Kokoro; nothing called them). Local turns now dispatch through the
  orchestrator's own provider, which is what tests inject (`tests/fake_providers.py`). Found while
  doing it: the model-facing schema comes from the Pydantic `*Args` classes in `TOOL_SCHEMAS`, not
  from signature introspection, and only the docstring's first line becomes the description.
  Pinned by `backend/tests/test_git_agent_tools.py`; 498 tests pass (the ~106 removed covered only
  deleted code).

- [x] **Closing to the tray (2026-09-23)** (`MainWindow.ShowFromBackground`, `App.ExitApp`,
  `Services/TrayService.cs`) — Closing the main window with X destroyed it while the process lived
  on (the HUD and the tray icon kept it alive), so neither the tray icon nor a second launch had a
  window to show: Jarvis became unreachable until killed. X now hides to the tray
  (`AppWindow.Closing` cancelled) while a tray icon exists; the tray's click and a second launch
  (single-instance redirect, now handled via `AppInstance.Activated`) show, restore and front the
  same window; "Exit Jarvis" is the one real exit and stops the backend with it; without a tray icon
  X falls back to a real close that ends the process. A first fix only covered a single click -- the
  user's double-click still did nothing, because H.NotifyIcon routes a double-click only to
  `DoubleClickCommand`; both now show the window (`NoLeftClickDelay` makes the single click
  immediate). Menu items moved from `Click` handlers to `Command`s, and `ForceCreate(false)` stops
  the tray icon from putting the whole process into Windows Efficiency Mode. Verified by sending
  WM_CLOSE and synthetic tray callbacks (single and double click); confirmed by the user on device.

- [x] **Liquid Glass motion measured off the iPad recording (2026-09-23, confirmed on device)** —
  Everything below is in
  `Jarvis.Glass` (the `Jarvis.GlassLab` bench got the morning's toggle material values and tap slop,
  but NOT the motion changes, so it no longer matches the app's motion).
  - *Toggle* (switch at t=64-75, three press-drag-release cycles):
  
    | Quantity | Measured | Was | Now |
    |---|---|---|---|
    | resting puck | 73x48 px, aspect 1.52 | 1.59 | 1.52 (radius 13.7, inset 2.85 -> 71.4 DIP track) |
    | lifted, still | 116x79 px: 1.41x track, aspect 1.47 | scale 1.46, aspect 1.83 | 1.65 / 1.47 |
    | tint / chromatic / magnify | ~6% / none / none | 0.08 / 0.12 / 0.2 | 0.06 / 0 / 0 |
    | travel settle | w 20, zeta 1.07, 10-90% 180 ms | one spring 520/30 (6% bounce) | 400/43 |
    | lift up (press) | w 35, zeta 0.8, 10-90% 70 ms | same shared spring | 1200/55 |
    | lift down (release) | w 19, zeta 1.1, 10-90% 215 ms | same shared spring | 350/41 |
    | speed stretch | +1.7% aspect / 100 px/s, and shorter | 1.2%/travel/s, wider only | 0.7%/travel/s, 40% width / 60% height |
    | overdrag | lens reaches 0.58 travel past an end, springs back | clamped | rubber band, limit 0.75 |
    | release timing | lens shrinks WHILE sliding back | shrank after landing | latch until 90% lifted, then free |
    | release frost | milky phase ~100 ms visible (72.07-72.17) | `ToggleLiftFrost` 5 | unchanged: qualitatively confirmed |
  
    Plus tap slop (`TapSlopDip` 6) so a touchpad click's wobble no longer eats the click.
  - *Slider* (t=36.5-63, ~570 resting / ~450 lifted frames): resting aspect 1.54 (was 1.4); lifted
    and still 112x81 px = 1.69x height, aspect 1.38 (was 1.35x, same aspect as rest); rail 6.75 DIP
    (6pt under a 24pt thumb; was 6); travel 700/53 critically damped (700/32 overshot); release
    225/24 (w 15, zeta 0.8); press borrows the toggle's lift-up (same ~80 ms); stretch ~1% per
    100 px/s up to +42%, 60% width / 40% height, per DIP/s; overdrag rubber band 15 DIP (24-29 px
    measured). The lens's jelly wobble (width/height trading after a fast stop) is NOT modelled.
  - *Segmented*: the recording has **no segmented control** (checked the whole 141 s). Its lift now
    uses the switch/slider press and release springs (both measured controls agree); its travel
    and stretch are unmeasured and deliberately unchanged (`PuckStretch` 0.024 = old effective value).
  - All three integrate springs in <= 1/240 s substeps (the stiffer press spring would ring on a
    50 ms dropped frame otherwise).

- [x] **Glass button measured (2026-09-23)** (`Jarvis.Glass/Controls/GlassButton.xaml.cs`) — The
  recording's one clean button press is a round Control Center module (t=30.8-33.2, 60 fps, no
  dropped frames). Radius fitted with a damped step: press pops 66 -> 79.5 px and back to 76.5
  (w 25, zeta 0.40, ~25% overshoot) then swells to 83.5 while held; release 83 -> 62.5 px, BELOW its
  66 px rest, and back (w 17, zeta 0.45, ~21%). Unlike the switch and slider, buttons bounce. Its
  glass also brightens x2.2 when pressed (luminance 41 -> 90), where ours went less white
  (`LiftTint` 0.05 -> 0.30). The bounce is expressed in size only (raw spring value drives the
  growth; material values are clamped 0..1). Size itself kept ours: that module grows 1.2-1.27x,
  too much for a wide pill. Springs integrate in 1/240 s substeps. Checked in the same pass and
  found unmeasurable: text-field focus (Spotlight's field appears already focused) and any
  segmented control (none in the recording).

- [x] **Housekeeping (2026-09-23)** — Deleted, with the user's go-ahead: the stale repo copy
  `jarvis_project/` (+ .zip), the old PyInstaller launcher output `dist/`, `build/`, `*.spec`
  (734 MB), old logs, root `__pycache__/`, and test debris in `workspace/`, `projects/`, `files/`;
  `DIFF.md`, `STAGE_B_AND_GOVERNOR_V2_DIFF_REPORT.md`, `JARVIS_PROJECT_SUMMARY.md` via git.
  The debris kept coming back because `ProjectIndexer` builds a default on-disk vector store under
  `WORKSPACE_PATH` (default `./workspace` = repo root in tests); `conftest.py` now points
  `WORKSPACE_PATH` at a temp dir like it already did for the database. `.env` / `.env.example`
  name the models that actually run (9B IQ3_XXS main, MiniCPM5-2B fast), and the never-read
  `MAIN_MODEL`, `FAST_MODEL`, `EMBEDDING_MODEL`, `RERANKER_MODEL` settings are gone. `README.md`
  corrected (models, local speech, close-to-tray, launch via the script); the pre-WinUI design
  docs carry a "historical" banner. 498 tests pass and leave the repo root untouched.

- [x] **Idle GPU: the capture echo (2026-09-23)** — Jarvis.App sat at ~40% 3D load with nothing
  on screen changing, and kept it while minimized. The render gate keyed on the capture frame
  count, and our own window, although excluded from capture, still makes DWM report its rect
  dirty and deliver a frame on every Present: render -> Present -> frame -> render, ~36 fps over
  a static desktop (the dirty rect was exactly our client rect). And `AppWindow.Changed` does not
  reliably report a minimize, so a minimized window rendered every frame too. Now
  `LiveCaptureSource` classifies each frame by its `DirtyRegions` (`ReportOnly`) against the
  window's rect: far away = ignored, near/overlapping (96 px margin) = render, wholly inside =
  "maybe", which `CaptureChangeDetector` settles by comparing the window's crop against a
  snapshot taken at the last backdrop render (ChangeDetect.hlsl discards identical texels inside
  an occlusion query, read back without stalling). `RenderTick` polls visibility itself and
  capture copies pause while hidden. The D3D device is now multithread-protected (the capture
  callback and the render tick shared the immediate context unguarded). Measured 40% -> 0.6%
  idle, ~1.5% with a window streaming behind; the user confirmed the glass still follows the
  desktop and is current after restore. `Jarvis.GlassLab`'s capture copy is unchanged.

- [x] **Spaces as tabs, spring transitions, a startup that materialises (2026-09-24)** — The
  brief: fluid switching between Freeform and Workspace, an entrance that covers the backend's
  boot, Apple-style springs; only transforms, opacity and glass layers from `GlassLayers`, no
  spinners or bezier ease-ins, 60 fps with no GPU spikes. Seen working on device by the user.
  - *Glass that moves, fades and materialises.* The glass is drawn under all XAML from shapes the
    controls publish, so animating an element's XAML used to leave its glass behind.
    `GlassMotion` (Jarvis.Glass) adds attached `Opacity` and `Material` values that `GlassScene`
    folds into every shape (a fade in `Params3.w` that the shaders scale coverage, rim and
    shadow by; material scales frost, tint and shadow: 0 = a clear lens, 1 = full glass) and
    into text alpha. Moving an element republishes only the controls inside it (ancestor sets
    captured at Loaded, so the always-on publish path pays nothing while nothing moves), and the
    host runs animation ticks before it renders, so glass and XAML move in the same frame.
    `GlassTransition`: translate, scale, opacity and material on springs stated as (response,
    damping ratio), 1/240 s substeps like the controls.
  - *Tabs* are the two spaces. A switch slides only the item panels (bubbles, session rows)
    36 DIPs toward the space being left, swaps content while hidden, slides the new ones in; the
    panels stay put and clip them, so only layer-2 rows re-render. Sheets rise 28 DIPs while the
    window's text dims on a spring. The lists' default `ItemContainerTransitions` are gone (they
    slid bubble XAML on the compositor while its glass stayed still).
  - *Startup*: the first version (a frosted card that lifted away) was "kinda normal", so it is
    now materialise + staged assemble: the card arrives as clear glass and condenses; a line
    fills on real steps (backend, workspaces, sessions); then the card swells and evaporates
    while sidebar, header, chat and composer rise in as clear glass 60 ms apart and frost a beat
    later. Whole-machine 3D GPU peaked at 12.8% in the hand-off (baseline ~7%).
  - *Startup CPU*: the user saw ~80% at launch -- a ~3 s Kokoro TTS warm-up (ONNX Runtime on all
    28 threads) landing on the window assembly. It now starts 8 s after the backend is ready
    (`KOKORO_WARMUP_DELAY_S`). Capping its threads was measured and rejected: the load took as
    long and replies synthesised 5-50% slower (RTF 0.55 -> 0.57-0.83).
  - *Startup time*: window open 15.4 s after launch -> 5.45 s. On Windows a connect to a closed
    localhost port takes ~2 s to be refused; `/health` probed llama-server's port on every call
    (2.2 s whenever no model was loaded) and `BackendHost` probed `/health` before launching
    uvicorn and then saw it ready only in 2 s steps. Now `/health` tries a 250 ms socket connect
    first, `BackendHost` launches uvicorn before building the windows and watches the TCP
    listener table, and the window polls the governor the moment the backend answers. `app.rag`
    resolves its names lazily, so `qdrant_client` (1.9 s) no longer loads at startup. uvicorn is
    restarted whenever it dies before answering, 1.5 s apart growing to 10 s, and past the
    window's 20 s wait it keeps going in the background (the machine intermittently refuses
    new sockets with WSAEACCES -- three times on 2026-09-24, once for more than 16 s). Tested
    by making `main.py` exit while a marker file existed: seven failed starts, the window opened
    offline at 25 s, the backend came up on the next start once the marker was gone.
  - *Shader cache*: each `GlassHost` compiled its HLSL from source at launch (0.67 s for the
    main window, 0.88 s again for the HUD's -- ~1.55 s of the ~4 s before the window showed).
    `ShaderPipeline` now keeps bytecode keyed by a hash of source + entry + profile, in memory
    and in `%LOCALAPPDATA%\Jarvis\ShaderCache` (virtualised into the package's own folder, so it
    is only visible to the app). Window shown 4.0 s -> 2.5 s after launch; the first launch after
    a shader edit compiles once. The reveal stays ~5.5 s: it waits on the backend.
  - *Backend's own start*: `/health` took ~280 ms even after the socket fix (a 250 ms connect
    timeout on the closed model port, plus loading all ~676 sessions and each one's first
    message just to count them) -- it is on the startup path twice and polled every 2 s. Now
    the model port is looked up in the OS's listener table (psutil, ~1 ms) and sessions are
    counted in SQL (`MemoryStore.count_sessions`): ~5 ms. Alembic (~0.9 s of imports with the
    SQL dialects it loads) is only imported when the SQLite database's stamped revision isn't
    the single head of `alembic/versions` (`_database_at_head`, read with sqlite3 and a regex;
    anything unexpected falls back to the full upgrade). The built-in MCP server connects in
    the background instead of before the backend answers (~0.26 s). Opens ~5.5 s -> ~4.5 s.
  - *Snapshot mode* (`scripts\snapshot-window.ps1`): scripted screenshots with the glass in them
    without turning capture exclusion off (a capturable window's glass captures itself and
    renders every frame). The app freezes the glass and springs, lifts exclusion on visible
    windows only -- and on the main window only while it is foreground -- and restores it.
  - Also: long workspace names trim to an ellipsis in the button and dropdown (`GlassText.MaxWidth`);
    the header sheds the toggles' captions (tooltips name them then), then the HUD button and
    title, by measuring its contents against its own width -- at the default 1280x800 it needed
    753 px of 637 and the Panel button was cut off.

- [x] **Governor: a paged-out model comes back (2026-09-24)** — When another process claims
  VRAM, WDDM demotes part of our model to shared memory (`model_resident=False`) and it stays
  there after the pressure passes: every token then crawls. The governor now counts polls with
  the model paged out; after 3, if nothing is running (no activity, llama-server not decoding,
  no throttle, not paused, 60 s since the last try) and the card's free VRAM covers the missing
  part plus the launch reserve, it restarts llama-server with the same model under a
  MODEL_LOADING activity (`on_paged_model_reload` -> `main.reload_paged_model`). The missing part
  is the model minus what's on the card, estimated against the desktop's pre-pressure share
  (the live share can't be measured while paged); with no baseline, room for the whole model is
  required. The launch ladder still sizes the relaunch and announces any downgrade. Tested with
  synthetic metrics only. Also: `test_governor.py` now runs on a stubbed idle machine
  (psutil/NVML patched) except the live-telemetry test -- two tests asserting IDLE had failed
  once while Jarvis was decoding on the card.

- [x] **Workspace awareness (2026-09-25, confirmed on device)** — Fixes for what the model knows
  about a project's files across turns (diagnosed 2026-09-24 on the Prompt Enhancer project,
  where it searched `*enhancer*`, found nothing, and wrote an unrequested TASKS.md).
  (a) Removed the "markdown code block + a filename in the text -> write_file(overwrite=True)"
  fallback from `extract_tool_calls_from_text`: in-workspace writes are LOW_RISK, so it wrote
  files unconfirmed. (b) Every WORKSPACE turn with a project carries the file list
  (`workspace_file_listing`, via `_space_context`, on the user turn -- it changes as files are
  written, so never in the cached prefix; max 50, skips `indexes/`/`memory/`). (c) Tool results
  are stored as `role=tool` rows capped at 2,000 chars, each carrying its own call, and
  `ContextManager` replays each as an assistant-call/tool-result pair that a budget cut can't
  split; rows without a call are skipped (a bare tool message breaks the template); the client
  hides them on reload. Found on the way: `get_messages(limit=)` returned the *oldest* rows, so
  past 20 messages every chat saw its opening turns forever -- now the newest, and tool rows
  don't count toward the 20. (d) One embedded Qdrant client per index folder for the process
  (`_SHARED_CLIENTS`, calls serialised under an RLock): the indexer's and retriever's separate
  clients locked each other out ("already accessed by another instance"), so semantic
  retrieval was always 0. Open: no project memories are ever written for a workspace.
