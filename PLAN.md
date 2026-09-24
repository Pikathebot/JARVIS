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
| Tests | pytest, 498 pass | No automated tests for the C# client. |

## 2. Status

Everything in `docs/MILESTONES.md` is built and committed. In short: streaming tool-calling agent
with honest tool turns; deterministic permissions with voice/typed confirmations and timeouts;
governor with relative VRAM floor and launch ladder; model catalogue with vision projectors and a
captioner fallback; stable prompt prefix, n-gram speculation, 32k context with compaction;
persona, hands-free voice, HUD, ambient awareness, routines and proactive actions; RAG with real
embeddings and a relevance floor; the WinUI client with Liquid Glass panels, controls, sheets,
workspace creation and image attachments; git tools for the workspace repository.

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

### 4.0 Startup: done for now

§4.0 (spaces, spring transitions, the materialising startup, startup CPU and time) is done --
see `docs/MILESTONES.md`. Window shows ~2.4 s after launch, opens ~4.5 s (three runs,
2026-09-24; was ~15.4 s that morning). What remains is almost all Python: uvicorn is spawned at
0.8 s and the backend's lifespan starts ~3.4 s later, ~2.7 s of it imports spread across ~2,000
modules (fastapi ~0.6 s, the agent/tools registry with sqlalchemy/sqlmodel/httpx ~0.8 s) --
no single big one left. Further cuts would mean restructuring imports broadly; not worth it
unless startup matters again.

### 4.1 Liquid Glass fidelity (in progress)

Source of truth: the user's iPad recording `ScreenRecording_09-22-2026 19-38-02_1.mp4`
(2778x1940 HEVC, in Downloads -- **keep it**; the WhatsApp copy is too compressed to measure).
Frames: `ffmpeg -ss <t> -i <video> -frames:v 1 -q:v 1 out.png`, then measure with PIL/numpy.
**t=72.0** = the switch with its puck lifted mid-drag (x 2444..2610, y 596..676);
**t=36.5 / 41.0** = the slider thumb at rest / lifted (not the switch -- different aspect);
**t=83.0 -> 85.0** = folder-open transition (background dim/blur).

**Measuring tools** (scratchpad copies are gone after the session; the method is what matters):
read a region frame by frame with each frame's real timestamp -- ffmpeg `crop,showinfo`,
`-fps_mode passthrough`, rawvideo to numpy; the recording is 60 fps with ~45 dropped-frame gaps,
so check gaps before fitting timing. Track a lifted lens by its TOP edge above the track/rail
against a resting frame or the flat card colour (the bottom is polluted by its shadow), height =
2 x (track centre - top), round ends corrected with the circle. Fit each channel with a damped
step response (scipy `least_squares`, damping ratio free). Traps hit: brightness thresholds
shave edges (measure at the colour midpoint); the switch's grey/green track change reads as lens
unless rows right above the track are excluded; the slider lens is jelly-like (width and height
swap while it moves), so only still frames give its shape.

**a. Done 2026-09-23 and confirmed on device:** toggle, slider and segmented-lift measurements
(tables in `docs/MILESTONES.md`).

**b. Glass button measured and applied 2026-09-23, confirmed on device** (Control Center
button, t=30.8-33.2): iOS buttons bounce -- press w 25 / zeta 0.40 (~25%), release w 17 / zeta 0.45
(~21%, dips below rest) -- and the pressed glass gets ~2.2x brighter (`LiftTint` 0.05 -> 0.30).
Size growth left at ours (that button is a round module growing 1.2-1.27x). Details in
`docs/MILESTONES.md`.

**c. Still open:** the segmented control (needs a short iPad recording of one); the slider
lens's jelly wobble; `GlassTextField` (the recording's only text field, Spotlight's, appears
already focused, so there is no focus animation to measure); `Jarvis.GlassLab` lags the app's motion.
The sheet backdrop fade was judged fine on device (2026-09-23) and stays at 0.08.

### 4.2 Voice accuracy (user-reported 2026-09-22, deferred by the user)

Mic pickup is inconsistent and transcripts are often wrong. What the code does today: client VAD
in `VoiceViewModel` (RMS threshold 0.045, 850 ms silence, 320 ms min, 12 s max), whole utterance
uploaded, backend transcribes with faster-whisper **`tiny.en`, beam 2, on CPU**
(`voice/transcriber.py`) with a prompt that teaches it the name. The cheapest real lever is the
model size -- `base.en`/`small.en` on the 20-thread CPU -- measured for latency and word error on
a few recorded utterances before picking; then VAD thresholds / gain. Wait for the user to pick
this up.

### 4.3 Housekeeping

Done 2026-09-23 (see `docs/MILESTONES.md`). Nothing open.

### 4.4 Robustness backlog

- **Intermittent WSAEACCES on new sockets.** Three times on 2026-09-24 (the user's first
  launch, 11:13, 12:32) every new TCP socket on the machine -- curl, PowerShell, Python, even a
  loopback bind -- failed with 10013 for seconds to more than 16 s. No trace in the System,
  Application, Firewall, Defender or HNS logs; no large excluded port ranges (`hns` and
  `vmcompute` run, so Hyper-V can reserve ranges transiently). The app now rides it out
  (`BackendHost` keeps restarting uvicorn). To find the cause, next time it happens run, as
  admin, `auditpol /set /subcategory:"Filtering Platform Connection" /failure:enable` and read
  Security events 5157/5159, plus `netsh int ipv4 show excludedportrange protocol=tcp` at once.
- **Tool selection is keyword matching** (`get_relevant_tools`). Phrasings without a trigger word
  get no tools, and "do/check/show..." offers all of them. Worth revisiting once there are real
  misses to measure against.
- **Proactive actions**: only VRAM-critical eviction and disk-space naming are wired; any new one
  goes through `AwarenessMonitor` actions.

### 4.5 Waiting on the device

**Paged-out model reload (2026-09-24, tests only):** when WDDM demotes our model to shared
memory and the pressure then passes, the governor restarts llama-server with the same model
(`_paged_reload_due` / `reload_paged_model`). Never exercised on the card -- it needs something
to grab VRAM while the model is loaded (a game, or a CUDA script), then exit. Look for "Model
paged out of VRAM ... reloading it resident" in `backend.log`.

Built and committed, not yet confirmed by the user on screen: creating a workspace from the
dropdown (c9d16c1), RAG relevance floor + embedder (b6fee7a), sidecars holding 0 VRAM (b45513f,
live since the 2026-09-23 10:36 restart), 4.1a above, and the Freeform/Workspace spaces with
ephemeral mode (ba778ab, 2026-09-23: backend tested and checked live against the real DB --
ephemeral turns left every row count unchanged, a Freeform turn declined a file request -- but
the sidebar switch, the header Ephemeral toggle, the per-space session lists and the saved
space are unseen on screen). Profile memories now ride on every turn, not only ephemeral ones.
The HUD keeps its own fixed session outside both spaces -- left for a dedicated HUD session.
Older reports whose status is unknown: the narrow-mode right panel covering its own close button,
and an intermittent white slab the HUD card's size at launch (2026-09-20). The slab was
reproduced once, in the first snapshot: clearing capture exclusion on the *hidden* HUD makes DWM
hand capture a blank slab at its rect (76,76 460x108) -- snapshot mode now lifts it on visible
windows only. No path in the code shows the HUD at launch (hidden in its constructor, shown only
by the toggle), so the on-screen report is unexplained; ask the user if it recurs.
Header fit at 1280x800 (2026-09-24): logged 753 px needed of 637, captions shed, fits in 586 --
unseen on screen.
