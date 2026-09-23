# Jarvis -- Plan

**Stack:** FastAPI backend + native `llama-server.exe` (llama.cpp, CUDA) + WinUI 3 client with a
D3D11 Liquid Glass renderer (`desktop-winui/`). Optional OpenRouter "Heavy Mode", off by default.
**Machine:** Windows 11, i7-14700HX (20 threads), RTX 4060 Laptop **8 GB**, 16 GB DDR5.
**Repo:** `D:\JARVIS`. How to build, run and not break it: `CLAUDE.md`. What was built and why:
`docs/MILESTONES.md`. This file is the current state and what comes next -- keep §4 accurate.

---

## 0. What Jarvis is for

A private assistant that lives on this one laptop: it answers and acts (files, apps, media, web,
shell) with every risky action gated behind an explicit yes, speaks and listens without the cloud,
notices things about the machine on its own and says so once, and never lets its own model starve
the rest of the desktop of VRAM. The UI should feel like Apple's Liquid Glass, measured against a
real iPadOS 26 recording rather than eyeballed.

Principles that have held up:
- **Local first, fail silent rather than fail to cloud.** Cloud paths (OpenRouter, edge-tts)
  exist only as explicit opt-ins.
- **Measure, then change.** Model quants, KV types, VRAM budgets, spring timings and glass
  constants were all settled by measurement on this machine; guesses have repeatedly been wrong.
- **The backend decides meaning, the client renders.** The seam is plain HTTP + SSE.
- **One channel for anything unprompted** (`AwarenessMonitor.emit`).

## 1. Architecture as it actually runs (2026-09-23)

```
 WinUI 3 app (Jarvis.App + Jarvis.Core + Jarvis.Glass)
   |  starts & supervises uvicorn in a Job Object (BackendHost)
   |  HTTP + SSE
   v
 FastAPI :8000 ── orchestrator ── tool selection ─ router ─ permissions ─ tool pipeline
   |                   |                                               (worker thread, timeouts)
   |                   ├── llama-server :8001  main: Qwen3.5-9B IQ3_XXS + Q8_0 mmproj, 32k ctx
   |                   |                       fast: MiniCPM5-2B Q4_K_M (text-only)
   |                   ├── captioner  :8002  SmolVLM-500M, CPU, only for models without vision
   |                   └── RAG ── embedder :8003  nomic-embed-text-v1.5, CPU ── FTS5 + Qdrant(local)
   ├── governor (NVML/psutil + per-process GPU counters) ── launch ladder, eviction
   ├── awareness monitor ── rules, routines, proactive actions ── SSE /api/awareness/stream
   ├── voice: faster-whisper tiny.en (CPU) in, Kokoro-82M (CPU) out, wake word + follow-up window
   └── SQLite (SQLModel + Alembic) + per-project workspace trees + JSON state in data/
```

| Area | What runs | Notes |
|---|---|---|
| Inference | `llama-server.exe` build 10689, `-fa on`, q8_0 KV, `--spec-type ngram-simple`, `--no-mmap`, `-ngl 99` | Only one chat model resident at a time; a "fast" verdict never swaps (runs on main, thinking off). |
| Main model | Qwen3.5-9B UD-IQ3_XXS + Q8_0 projector @ 32k | ~5.0 GB; 25-29 tok/s decode. Chosen over Q3_K_XL (+4.7% PPL) because the real desktop holds ~2.3 GB VRAM. |
| Fast model | MiniCPM5-2B Q4_K_M | 5/5 on the tool-chain bench, 2.5x faster than the 4B. EN/ZH only, no vision. |
| Model choice | `data/models.json` via Settings -> Models | Overrides `.env`, which is stale on model paths. |
| Speech | STT faster-whisper `tiny.en` beam 2; TTS Kokoro-82M fp32 | Both CPU. edge-tts only with `VOICE_TTS_BACKEND=edge`. |
| RAG | nomic-embed sidecar + BM25 FTS5, relevance floor/gap/peak cuts | Hashed-vector fallback if the GGUF is missing. |
| Client | WinUI 3, .NET 10, WindowsAppSDK 2.4, packaged dev MSIX | Glass = SwapChainPanel under XAML, live desktop capture, layered HLSL passes. |
| Tests | pytest, 584 pass / 9 skip | No automated tests for the C# client. |

## 2. Status

Everything in `docs/MILESTONES.md` is built and committed. In short: streaming tool-calling agent
with honest tool turns; deterministic permissions with voice/typed confirmations and timeouts;
governor with relative VRAM floor and launch ladder; model catalogue with vision projectors and a
captioner fallback; stable prompt prefix, n-gram speculation, 32k context with compaction;
persona, hands-free voice, HUD, ambient awareness, routines and proactive actions; RAG with real
embeddings and a relevance floor; the WinUI client with Liquid Glass panels, controls, sheets,
workspace creation and image attachments.

## 3. Key endpoints

| Area | Endpoints |
| :--- | :--- |
| Chat | `POST /chat`, `POST /chat/stream` (SSE), `GET /sessions`, `GET/DELETE /sessions/{id}...` |
| Projects & files | `/api/projects...`, `POST /api/upload`, `/api/artifacts...` |
| Models | `GET /api/models`, `POST /api/models/select`, `DELETE /api/models/select/{slot}`, `POST /models/{load,unload}` |
| Governor | `GET /governor/status`, `POST /governor/{pause,resume,force-reload,...}`, `GET /health` |
| Confirmations | `POST /api/confirmations/{id}/deny` |
| Persona | `GET/POST /api/persona`, `PATCH/DELETE /api/persona/overrides`, `POST /api/persona/speech-preview` |
| Voice | `POST /api/voice/listen`, `POST /api/voice/say`, `POST /api/voice/session/{id}/{start,stop,arm,state}`, `GET /api/voice/hands-free` |
| Awareness | `GET /api/awareness/{status,observations,briefing,config,stream}`, `POST /api/awareness/poll`, `PATCH /api/awareness/config` |
| Routines | `GET/POST /api/routines`, `PATCH/DELETE /api/routines/{id}`, `POST /api/routines/{id}/run` |

---

## 4. Next Milestones

In priority order. Each item says what "done" means and what still needs the user.

### 4.1 Liquid Glass fidelity (in progress)

Source of truth: the user's iPad recording `ScreenRecording_09-22-2026 19-38-02_1.mp4`
(2778x1940 HEVC, in Downloads -- **keep it**; the WhatsApp copy is too compressed to measure).
Frames: `ffmpeg -ss <t> -i <video> -frames:v 1 -q:v 1 out.png`, then measure with PIL/numpy.
**t=72.0** = the switch with its puck lifted mid-drag (x 2444..2610, y 596..676);
**t=36.5 / 41.0** = the slider thumb at rest / lifted (not the switch -- different aspect);
**t=83.0 -> 85.0** = folder-open transition (background dim/blur).

**a. Confirm today's two changes on device, then commit** (built clean, *unverified on device*):
- `GlassToggle.Material` set to the t=72 measurements (both `Jarvis.Glass` and `Jarvis.GlassLab`):

  | Quantity | Measured | Was | Now |
  |---|---|---|---|
  | interior white wash | ~6% | `ToggleLiftTint` 0.08 | 0.06 |
  | lens height / track height | 1.43 | `ToggleLiftScale` 1.46 (=1.23) | 1.69 |
  | lens aspect w/h | 1.45 | `ToggleLiftAspect` 1.83 | 1.45 |
  | chromatic fringing | none | `ToggleLiftChromatic` 0.12 | 0 |
  | magnification | none | `ToggleLiftMagnify` 0.2 | 0 |
  | rim band / half-height | 0.45 | `ToggleLiftBezelFraction` 0.46 | unchanged |
  | rim bend | outward | `ToggleLiftRefraction` -9 | unchanged |

  The lens now overhangs the 32-DIP track by ~7 DIP each side; a toggle within 7 DIP of a
  ScrollViewer edge would have its lens clipped -- check the Settings sheet's first/last rows.
- Tap slop (`TapSlopDip` = 6) in `GlassToggle` and `GlassSegmented`, both copies. The 09-22 drag
  fix made any press that moved >=2 DIP a "drag"; a touchpad click's wobble then settled back
  where it started and the click was lost ("the button is not responsive like before"). A release
  is now a drag only if the pointer got >= 6 DIP away at some point.

**b. Spring timing -- needs a decision with the user.** The recording gives lift ~130 ms, settle
~130-140 ms, **no overshoot** (lens height tracked over 120 frames through two press-drag-release
cycles). Ours is stiffness 520 / damping 30: ~270 ms with deliberate overshoot, and the code
comment claims that overshoot came from a recording. Critically damped at ~130 ms is roughly
stiffness 2000 / damping 90. Only the lift channel was measured; measure the travel channel (puck
sliding side to side) before changing it -- it may legitimately differ.

**c. Still unmeasured:** `ThumbStretch` 0.012 (no velocity stretch seen, but width-vs-velocity was
not checked); `ToggleLiftFrost` 5 (the milky phase on release); all of `GlassSlider` (rest aspect
measures 1.54, lifted 1.79 vs our `ThumbAspect` 1.4) and `GlassSegmented`.

**d. Sheet backdrop dim (optional polish).** `SheetBackdropOpacity` (`MainWindow.xaml.cs`) fades
the window's XAML to 0.08 while a sheet is open -- set before the layer fix, likely too aggressive
now. Apple dims behind an opened folder by 19% with a ~30-35 px sigma blur, but ours compensates
for XAML painting above the swapchain, so the right value is a judgement on device, not 0.81.

### 4.2 Voice accuracy (user-reported 2026-09-22, deferred by the user)

Mic pickup is inconsistent and transcripts are often wrong. What the code does today: client VAD
in `VoiceViewModel` (RMS threshold 0.045, 850 ms silence, 320 ms min, 12 s max), whole utterance
uploaded, backend transcribes with faster-whisper **`tiny.en`, beam 2, on CPU**
(`voice/transcriber.py`) with a prompt that teaches it the name. The cheapest real lever is the
model size -- `base.en`/`small.en` on the 20-thread CPU -- measured for latency and word error on
a few recorded utterances before picking; then VAD thresholds / gain. Wait for the user to pick
this up.

### 4.3 Housekeeping (needs the user's go-ahead; nothing is broken without it)

- **Dead second tool stack.** `backend/app/tools/` (21 `BaseTool`s: git, unreal, terminal, patch,
  web, vision) is registered in `main.py` but consumed only by `AgentLoop`, which is never run --
  the model has never been offered these tools. Options: delete them with `agent/loop.py`, their
  tests and the `tool_registry` wiring; or port the useful ones (git status/diff/log) into
  `app/agent/tools/` with risk tiers and triggers. Same question for `ollama_provider.py`,
  `lmstudio_client.py` (engines not installed) and `agent/tts/chatterbox_engine.py` (disabled).
- **Stale configuration and docs.** `backend/.env` and `.env.example` name models that are not
  what runs (`LLAMA_*_MODEL_PATH`, `MAIN_MODEL`, `FAST_MODEL`, `EMBEDDING_MODEL`, `RAG_*_MODEL`);
  `docs/ARCHITECTURE.md`, `AI_AGENT_CONTEXT.md`, `API_REFERENCE.md`, `README.md` predate the
  model changes. Either correct them or mark them historical.
- **Repo-root debris.** `DIFF.md`, `STAGE_B_AND_GOVERNOR_V2_DIFF_REPORT.md`,
  `JARVIS_PROJECT_SUMMARY.md`, `jarvis_project/` + `.zip`, `dist/`, `build/`, `*.spec`,
  root `workspace/` (tests write `MagicMock`/`p` project dirs there -- a test-isolation bug worth
  fixing on its own), old `*.log` files.

### 4.4 Robustness backlog

- **Paged-out model.** When another app claims VRAM, WDDM demotes our model to shared memory
  (`model_resident=False`) and inference crawls. The governor only avoids corrupting its baseline
  in that state; it should reload once the pressure passes.
- **Tool selection is keyword matching** (`get_relevant_tools`). Phrasings without a trigger word
  get no tools, and "do/check/show..." offers all of them. Worth revisiting once there are real
  misses to measure against.
- **Proactive actions**: only VRAM-critical eviction and disk-space naming are wired; any new one
  goes through `AwarenessMonitor` actions.

### 4.5 Waiting on the device

Built and committed, not yet confirmed by the user on screen: creating a workspace from the
dropdown (c9d16c1), RAG relevance floor + embedder (b6fee7a), sidecars holding 0 VRAM (b45513f,
live since the 2026-09-23 10:36 restart), and 4.1a above. Older reports whose status is unknown:
an intermittent white slab the HUD card's size at launch (2026-09-20), and the narrow-mode right
panel covering its own close button.
