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

### 4.6 Tool review with the user (started 2026-09-26)

Going through the model's tools one at a time as a conversation with the user; the points for
each are noted here and fixes are applied at the end (or next session), not mid-discussion.

Done before the review started (2026-09-26, tests only): `kill_process` always asks (it was
LOW_RISK for any single non-critical process); `stop_playback`/`play_audio` removed from the
tool tables (speech plays in the client, the backend never plays audio); `git_commit` takes an
optional `files` list and commits only those.

Queued for discussion: keyword tool offering (`get_relevant_tools`: action words offer all
tools, "open"/"task" offer every OS tool, offered tools accumulate per session); missing tools
(routines from chat, screen look, open a URL in the browser).

Per-tool notes:
- `read_file` (agreed): (1) outside the workspace it asks, and once approved it reads -- today
  the permission layer asks but the tool refuses anything outside the workspace anyway, so the
  approval is pointless; the tool needs to know the call was approved. (2) Keep the "search the
  workspace for that file name" fallback, but say so in the result ("wasn't at that path; this
  is src/config.json"). (3) Check whether `projects.local_folders_json` is used; if it is,
  linked folders count as inside the workspace. (4) Binary files: refuse with type and size
  instead of returning replacement characters.
- `list_directory` (agreed): (1) same as read_file -- asks outside the workspace, lists once
  approved. (2) Size and modified date on files. (3) Known bulk folders (`node_modules`, `.git`,
  `__pycache__`, `.venv`/`venv`, `bin`/`obj`, `dist`/`build`, ...) are shown as one line with
  their entry count, not expanded; plus a hard cap (~200 entries, then "...and N more, use
  find_files"). (4) Folders and files in separate groups, folders first.
- `find_files` (agreed): (1) skip only clutter folders (`.git`, `node_modules`, `__pycache__`,
  `.venv`, `bin`/`obj`) -- today it also hides every `data`/`build`/`dist` folder and every
  dot-folder, and every image/media/archive/db/exe file, so `*.png` finds nothing; the
  extension skip belongs to grep only. (2) Match folders too (the docstring already says so).
  (3) Same as read_file outside the workspace. (4) Size + modified date per result, as in
  list_directory (newest first).
- `grep_in_files` (agreed): (1) skip files over a few MB and detect binaries by sniffing the
  first bytes, not by extension (`.gguf`/`.bin`/`.pdf`/`.safetensors` are read whole today).
  (2) Cut a matching line to ~200 chars around the match (minified JS / one-line JSON floods
  the result). (3) Always show a small snippet: one line above and below each match. (4) Same
  clutter-only folder skip as find_files. (5) Same as read_file outside the workspace.
  (6) **Later session:** search inside PDFs / Word docs (needs text extraction).
- `write_file` (agreed): (1) creating a new file runs; **overwriting an existing file asks**, and
  the old version is copied to `.jarvis/backups/` in the workspace first so it can be restored
  (today it overwrites silently, LOW_RISK). (2) Outside the workspace: allowed once approved,
  and the card must say **why** -- add a short `reason` arg the model fills, shown on the
  confirmation card (for overwrites too). (3) Encoding (UTF-16/BOM becomes UTF-8): leave it.
- `patch_file` (agreed): (1) **bug:** re-index the file for RAG after a patch (write_file does,
  patch_file doesn't, so workspace search returns the pre-edit text). (2) When the search block
  isn't found, show the closest-matching region of the file, not its first 400 chars.
  (3) Keeps running without asking, but backs the old version up to `.jarvis/backups/` like
  write_file. (4) Wrong-file fallback says which file it patched (as read_file). (5) Optional
  `replace_all`. (6) Outside the workspace: as write_file (asks, with a reason on the card).
- `delete_file` (agreed): (1) send to the Windows Recycle Bin instead of `os.remove`.
  (2) **bug:** drop the file from the RAG index (deleted files stay searchable today).
  (3) Folders too -- Recycle Bin, asks, card shows how many files are inside. (4) Outside the
  workspace: as write_file. (5) `reason` arg shown on the card.
- `execute_command` (agreed; **(1) is the highest-priority fix of the review**): (1) **security
  bug:** `SAFE_COMMAND_PREFIXES` is a prefix match, so `echo hi; Remove-Item -Recurse ...`,
  `dir && del x`, `echo "" > notes.md`, `echo $(Stop-Computer)` run without asking. Safe only
  when it is one plain command: no `;`, `&&`/`||`, `|`, `>`/`<`, `$(`, backtick, newline.
  (2) A "safe" command must only touch paths inside the workspace (`cat C:\x\secret.txt`
  bypasses read_file's gate today). (3) Timeout 30 s -> ~2 min, and return the output
  captured before a timeout. (4) `reason` arg on the card -- matters most here. (5) Add
  PowerShell spellings to `DANGEROUS_COMMAND_PATTERNS` (`Remove-Item -Recurse`, `Format-Volume`,
  `Stop-Computer`/`Restart-Computer`, `Clear-Disk`, ...).
- Artifacts (`create_`/`update_`/`read_artifact`, agreed): (1) **the user hit this in real
  chats:** the model can't find an artifact again (update/read need an ID and nothing lists
  them) -- list the workspace's artifacts (name + ID, ~20 most recent) in each turn's context,
  like memories. (2) Rule for when to use one, Claude-style, in the tool description / protocol
  rules: an artifact is substantial, self-contained content the user will reuse, edit or keep
  (plan, report, spec, documents the workspace needs, longer code meant to be read/iterated);
  short answers/snippets/explanations stay in chat; anything that must live on disk to be used
  (a script to run, config, a change to project code) is a file. (3) `patch_artifact`
  (find/replace like patch_file) instead of resending the whole content. (4) `read_artifact`
  scoped to the current workspace.

**Resume here (next session):** the git family (`git_status`/`git_diff`/`git_log`/`git_commit`/
`git_checkout`) was presented, the user hasn't answered yet. Points raised: (1) split
`git_checkout` into `git_switch` (branch) and `git_restore` (file; the card says it discards
uncommitted changes, backup to `.jarvis/backups/` first); (2) no way to create a branch
(`git_switch create=true`); (3) large diffs arrive cut in the middle -- show `--stat` first when
big; (4) no push/pull -- deliberate? (suggested: push left out or card-only; pull risky with
conflicts). After git: `web_search`, `fetch_url`, then the OS tools (`launch_app` has the
Temp-escape bug noted above), `get_system_status`, `remember`/`forget`, then points 3 and 5
(keyword offering, missing tools).
- `launch_app`: **bug** -- `permissions.py` ~309 checks `"\temp\\"`, `"\tmp\\"`,
  `"\appdata\local\temp\\"` in plain strings: `\t` is a tab and `\a` a bell, so the Temp
  escalation never matches.
- `stop_playback` leftovers: `tools/audio_playback.py` is dead (nothing calls `play_audio`);
  only `main.py`'s shutdown call and the always-false `is_playing_audio` status field
  (unused `IsPlayingAudio` in `ApiModels.cs`) keep it alive.

### 4.7 Design system v2 (started 2026-09-27)

Spec: the "Jarvis" Design System artifact (https://claude.ai/artifact/Hf6FFbtmz7dFhnTrBoERzQ) --
tokens, guidelines, component cards, an audit of v1 and a migration map from every old literal.
Decisions: iOS 26 + "calm instrument"; iOS blue as the one accent; iOS status colours with
icon + word; a desktop type scale (body 13, messages 15, floor 11; not Dynamic Type); themes
dark / light glass / high contrast; Segoe Fluent icons; new mark (blue orb behind a glass pane).

**a. Code phase 1 -- tokens into the app (2026-09-27, build-verified):**
- `Jarvis.Glass/Theme/JarvisPalette.cs`: every colour per appearance, the one source. GlassLab
  links the file (still no project reference).
- `Jarvis.App/Themes/JarvisTheme.cs` builds `{Token}Brush` / `{Token}Color` theme dictionaries
  from it (Dark, Light, and HighContrast for Windows' own HC); Jarvis's HC recolours the Dark
  brushes in place. `Themes/Typography.xaml`: text styles, radii, control sizes.
  `Themes/StatusStyle.cs`: status kind -> colour + glyph.
- `GlassSlab.Material` (Regular / Thick / Clear / Accent): tint colour from the palette, tint
  floors per appearance (dark unchanged), HC outline and no shadows.
- Every XAML/C# colour literal, font size and emoji in Jarvis.App migrated; Settings has an
  Appearance switch (Dark | Light | High contrast), saved in LocalSettings.

Runs on the device (2026-09-27): dark seen in a snapshot; the user switched to Light and High
contrast and both work -- fine-tuning of those two is deferred. Saved choice across restarts
not yet confirmed. Two startup crashes fixed on the way: one ResourceDictionary under both "Dark" and
"Default" (WinUI refuses it), and `Application.Resources` throwing E_UNEXPECTED in the App
constructor -- `JarvisTheme.Install` now runs at the top of `OnLaunched`.

**c. Next -- the AppLayout (approved by the user 2026-09-27):** the artifact's `AppLayout` card.
Plain-row sidebar (date groups, search, compose icon, hover "..."); toolbar as clear-glass
capsules in the caption row; one status pill (model + VRAM) whose popover holds meters,
Governor, fast model, Free VRAM, Models...; one thick reading surface; 720 px transcript,
assistant turns without a bubble, one-line tool-step rows; composer as one capsule (attach,
field, mic, round send/stop); inspector (old right panel) closed until an artifact/context
arrives; Ephemeral as a toolbar eye. Popover must be inline glass, not a Flyout.

**b. Still open:** adaptive glass (renderer: per-slab luminance, flip small chrome between
dark/light, raise tint on reading areas -- thresholds in the artifact); app tile PNGs from
`Assets/Brand/jarvis-app-tile.svg` (manifest assets are still the template cross); awareness
toasts as glass (they are XAML borders, and a sibling slab would share the messages layer).

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
