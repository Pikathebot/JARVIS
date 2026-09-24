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

### 4.0 Next session: tabbed navigation, spring transitions, startup sequence (queued 2026-09-23)

The user's brief, for `desktop-winui/Jarvis.App`:
1. **Tabbed navigation:** `MainWindow` hosts distinct sections (Freeform, Workspaces,
   Settings/sheets) with fluid switching -- builds on the Freeform/Workspace spaces landed the
   same day (`ChatViewModel.SwitchSpaceAsync`, the sidebar `SpaceSwitch`).
2. **Startup sequence:** a smooth window entrance that covers the BackendHost/uvicorn boot and
   resolves into the saved space once the backend is up and state is loaded
   (`GovernorViewModel.BackendCameOnline` is the existing "backend ready" signal; projects and
   sessions load from it).
3. **Apple-style spatial transitions:** tab shifts driven by springs (mass/stiffness/damping),
   not linear or cubic-bezier curves. The damped springs in `GlassButton`/`GlassToggle`
   (fitted to the iPad recording, 1/240 s substeps) are the house pattern to reuse.

Constraints from the brief: glass layers only via `GlassLayers` constants; no glass in
popups/flyouts; XAML always paints above the swapchain. Not allowed: circular spinners, abrupt
cubic-bezier ease-ins, unthrottled Gaussian blur spikes, layout-shifting animations (animate
transforms and opacity only). Frames must hold 60 fps with no GPU spikes in Jarvis.Glass --
remember every glass shape that moves republishes to `GlassScene` and re-renders its layer (and
everything above it) each frame, and frost is the expensive pass, so animate as few glass
shapes as possible and measure with the per-process 3D counter (`Get-Counter '\GPU Engine(*engtype_3D)\Utilization Percentage'`, filtered by pid).

Done means: tabs switch with smooth spatial transitions, no z-fighting or clipping against the
swapchain; startup resolves cleanly to the initial tab when the backend is ready;
`dotnet build Jarvis.slnx` 0 errors. Needs the user: the physical feel and frame pacing can only
be judged on the device (the glass can't be screenshotted) -- ask them to run it.

**Built 2026-09-24.** Seen on device the same day: the space transitions ("good"), Settings
opening/closing, the workspace dropdown. Not yet commented on: the startup sequence itself, and
GPU during transitions. In one earlier run the dropdown's rows (and Settings) didn't draw while
still taking clicks; that run coincided with a machine-wide, intermittent block on new TCP
connections (curl, PowerShell and Python all got WSAEACCES, cleared by a restart) and did not
recur. `MainWindow.LogDropdownGlassAsync` logs the scene per layer to `jarvis-app.log` on each
dropdown open -- remove it once the dropdown has stayed good for a while. Build 0/0; the three
changed shaders compile under fxc ps_5_0. What was built:
- *Glass can now move and fade with XAML.* `GlassMotion` (Jarvis.Glass): an attached glass
  opacity that `GlassScene` folds into every shape's new fade (`Params3.w`: coverage, rim and
  shadow scale by it in the shaders) and every text run's alpha; scoped republish of the controls
  inside a moved element (ancestors captured once at Loaded, so the always-on publish path pays
  nothing while nothing is faded); an animation tick the host runs *before* rendering, so glass
  and XAML move in the same frame. `GlassTransition`: translate + opacity on damped springs
  stated as (response, damping ratio), same 1/240 s substeps as the controls.
- *Startup:* the panels start invisible (glass too) and lowered; a frosted "JARVIS" card on the
  sheet layer shows "Starting up" with a breathing caption (a XAML storyboard, so the wait costs
  no GPU). Backend online -> projects -> the first session-list fetch (capped 1.5 s) -> the card
  lifts away and the sidebar, then 50 ms later the chat column, rise 18 DIPs into place. Held at
  least 450 ms so a warm backend doesn't flicker; opens anyway after 25 s offline.
  `jarvis-app.log` records "startup: revealing (...) after N ms". User's verdict: "kinda normal"
-- to be replaced by materialise + staged assemble (clear glass whose frost and tint come in, a
real-step progress line, panels materialising one by one), built with snapshot mode.
- *Tabs* = the two spaces on the sidebar switch (Settings stays a sheet -- the brief's
  "Settings/sheets"). A switch slides the message list's and session list's item panels 36 DIPs
  toward the space being left and fades them out (0.22 s, critically damped), swaps transcript
  and sessions while hidden (capped 600 ms), then slides the new ones in from the other side
  (0.42 s, damping 0.86). The panels stay put and clip the moving bubbles, so only layer-2 rows
  re-render. A switch overtaken mid-flight bows out to the newer one.
- *Sheets* (Settings, New workspace) fade up while the sheet rises 28 DIPs; the window's text
  dims to 8% on a spring (XAML only, as before), replacing the 160 ms cubic fade.
- Both lists' default `ItemContainerTransitions` are removed: they animated each bubble's XAML
  on the compositor while its glass stayed still. New messages now appear without that slide.

To check on device: startup cold and warm; Freeform <-> Workspace repeatedly and rapidly;
Settings open/close; GPU per process during each (`Get-Counter '\GPU Engine(*engtype_3D)\Utilization Percentage'`
filtered by pid) -- the startup reveal moves frosted panels, so it re-blurs layer 1 each frame
for ~0.6 s; if that spikes, cache the frost per layer (it depends only on the layer below).

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

- **Done 2026-09-24: startup CPU burst.** The user saw ~80% CPU at launch. Measured (per-process
  counters, 1 s): a ~3 s burst from the Kokoro TTS warm-up (ONNX Runtime on all 28 threads,
  62-87% of the machine), plus ~50 s of `dotnet build` when the launcher rebuilds after code
  changes. The warm-up now starts 8 s after the backend is ready (`KOKORO_WARMUP_DELAY_S`), past
  the client's window assembly; confirmed live: reveal 10:31:52.5, Kokoro load 10:31:57.7.
  Capping its threads was measured and rejected (load no shorter; replies RTF 0.55 -> 0.57-0.83).

- **Paged-out model.** When another app claims VRAM, WDDM demotes our model to shared memory
  (`model_resident=False`) and inference crawls. The governor only avoids corrupting its baseline
  in that state; it should reload once the pressure passes.
- **Tool selection is keyword matching** (`get_relevant_tools`). Phrasings without a trigger word
  get no tools, and "do/check/show..." offers all of them. Worth revisiting once there are real
  misses to measure against.
- **Governor tests read the real GPU.** `conftest.py` stubs a healthy governor for every test
  except `test_governor.py`, so two of those failed once while Jarvis itself was running on the
  card (they pass alone and on an idle GPU). Give them a stubbed metrics source.
- **Proactive actions**: only VRAM-critical eviction and disk-space naming are wired; any new one
  goes through `AwarenessMonitor` actions.

### 4.5 Waiting on the device

Built and committed, not yet confirmed by the user on screen: creating a workspace from the
dropdown (c9d16c1), RAG relevance floor + embedder (b6fee7a), sidecars holding 0 VRAM (b45513f,
live since the 2026-09-23 10:36 restart), 4.1a above, and the Freeform/Workspace spaces with
ephemeral mode (ba778ab, 2026-09-23: backend tested and checked live against the real DB --
ephemeral turns left every row count unchanged, a Freeform turn declined a file request -- but
the sidebar switch, the header Ephemeral toggle, the per-space session lists and the saved
space are unseen on screen). Profile memories now ride on every turn, not only ephemeral ones.
The HUD keeps its own fixed session outside both spaces -- left for a dedicated HUD session. Older reports whose status is unknown:
an intermittent white slab the HUD card's size at launch (2026-09-20), and the narrow-mode right
panel covering its own close button. Lead on the white slab (2026-09-24): the first snapshot
showed exactly that slab at the hidden HUD's rect (76,76 460x108) once its capture exclusion was
cleared -- DWM keeps a blank surface for the hidden HUD. If the launch report recurs, suspect
the HUD's show/affinity order at startup.

**New, from the first snapshot (2026-09-24):** at the default 1280x800 window the header
overflows -- the Panel button is cut off at the right edge. `ApplyResponsiveLayout` sheds
captions below 700 px of *window* width, but the header's own available width (window minus
sidebar) is what runs out.
