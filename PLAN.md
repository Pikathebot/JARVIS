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

- Git family (`git_status`/`git_diff`/`git_log`/`git_commit`/`git_checkout`, agreed
  2026-09-30): (1) split `git_checkout` into `git_switch` (branch) and `git_restore` (file; the
  card says it discards uncommitted changes, backup to `.jarvis/backups/` first); (2) create a
  branch with `git_switch create=true`; (3) large diffs: show `--stat` first, then the diff of
  one file on request, instead of cutting in the middle; (4) **add `git_push` and `git_pull`**
  (missing by accident, not by design) -- both ask; pull as `--ff-only` so a conflict never
  starts mid-chat (it reports "can't fast-forward" instead).

- `web_search` (agreed 2026-09-30): (1) stop keyword-gating it --
  offer `web_search` + `fetch_url` on every turn (LOW_RISK, small schema, and a fixed tool set
  is better for the byte-stable prefix than tools appearing mid-session), with a protocol rule:
  search when the answer depends on facts that may have changed since training (current
  holders of roles, prices, versions, news, anything "latest/today"), and say so when answering
  from memory; check with a small set of should-search / shouldn't-search prompts that it
  doesn't over-search (the fast model too). (2) Agreed: if the snippet doesn't state the fact, `fetch_url` the best
  result before answering. (3) Agreed: `news=true` (DDG news: date + source), date shown on
  every result. (4) Agreed: answers that used the web end with the links used (tool
  description + protocol rules). (5) No confirmation card; show the query on the
  collapsed tool-step row ("Searched: ...") -- today it is only in the expanded arguments -- and
  a rule that queries are short search terms, never pasted document or chat text.

- `fetch_url` (2026-09-30; the user delegated the call -- "choose which best fits", all five
  taken): (1) **security:** fetched text is marked as untrusted page data (tool description +
  protocol rules), and `fetch_url` **asks** when the URL didn't come from the user's message, a
  search result, or the link list of a page already fetched this session (a model-built URL
  is the exfiltration path: `?q=<document text>`). (2) **SSRF:** check every redirect hop and
  the resolved IP, not just the first hostname (a public URL redirecting to `localhost:8000`
  gets through today). (3) Read further: an `offset` to continue past the cut, and the result
  states the page's total length. (4) PDFs: extract text (or say "PDF, N pages" if no
  extractor) -- ties in with grep_in_files (6); JS-only pages: say the text couldn't be read
  instead of returning menu junk. (5) Links as a numbered list at the end (cap ~30), not
  inline -- saves the char budget and feeds the "links of fetched pages" list in (1).

- `launch_app` (agreed 2026-09-30): (1) **the user's bug** (log 2026-09-29 10:55: the model
  called `launch_app("qwen studio")`; the shortcut is `Qwen.lnk`, and `find_start_menu_shortcut`
  only matches the whole phrase as equal/prefix/substring, so nothing matched): match word by
  word, and when there's no clear winner return the closest installed apps ("No 'qwen studio'.
  Closest: Qwen, Unsloth Studio, Android Studio") so the model picks or asks. (2) Several
  matches and none exact ("studio") -> ask which one, instead of silently taking the shortest
  name. (3) Store/AppX apps: resolve from the Start menu's own app list (`Get-StartApps`
  equivalent, launch via `shell:AppsFolder\<AppID>`), not only the `.lnk` folders. (4) The
  Temp-escape bug below (raw strings).

- **The user's "forgotten 500 rule" -- diagnosed 2026-09-30 (a `forget` bug, not a context one):**
  memory `bbb9c9b9` ("prompts under 500 words", global preference) was saved 09-26 and replaced
  correctly. On 09-29 10:56, in the Freeform "open qwen studio" chat, the user wrote "i gave the
  wrong name bro its actually called qwen"; the model read that as a memory correction and ran
  forget(bbb9c9b9) -> remember -> forget -> remember -> forget in one turn, ending with the rule
  deleted (log 103214-103271; no forget cue matched -- the model did it unprompted). Same chat,
  turn 2 ("open qwen"): the model said "I attempted to open Qwen" **without calling any tool**
  (`launch_app("qwen")` would have matched `Qwen.lnk`). Memory restored by hand 2026-09-30 as
  `7586e944` (same text, global preference). Fixes (agreed 2026-09-30): (a) `forget` refuses unless the user's message asks to
  forget/correct something (the `_FORGET_CUE_RE` cue, widened) -- or asks; (b) forgotten
  memories go to a restorable bin (Settings), not a hard delete; (c) loop guard: a turn that
  forgets and re-saves the same fact stops after the first pair; (d) claimed-but-not-done
  actions ("I attempted to open X" with no tool call) -- protocol rule, and consider a check.
  **Built 2026-10-01 (tests pass, unverified on device):** (a) `asks_to_forget` in
  `tools/memory.py`, the real user message injected by `execute_tool` (a model-supplied one is
  ignored); (b) `app/memory/memory_bin.py` -> `data/memory_bin.json`, 30 days, `GET
  /api/memories/bin` + `POST .../bin/{id}/restore` (no UI yet -- the "Forgot: ... Undo" chat line
  is still to do); (c) `remember` refuses a fact forgotten earlier in the same turn; (d)
  `claims_unrun_action` in the orchestrator: a reply claiming an action with no tool run gets
  sent back once with the tools still offered (both loops).

- `focus_app` (agreed 2026-09-30): (1) it reports success without checking -- Windows'
  foreground lock usually stops a background process (the backend) from raising a window (it
  flashes in the taskbar; unverified on device): verify with `GetForegroundWindow` after the
  call and say so honestly, and have the WinUI client (foreground while the user types) call
  `AllowSetForegroundWindow` for the backend. (2) Match order: process name, then exact title,
  then title substring; several candidates -> list and ask (as launch_app). (3) **`launch_app`
  focuses an already-running app instead of starting a second copy**, so "open X" is one call;
  `focus_app` stays for "switch to X". (4) Word-by-word matching with closest candidates, as
  launch_app.

- Audio (`set_volume`/`mute_toggle`/`media_key`, 2026-09-30): (1) relative change (`change=+10`)
  besides an absolute level, result always "Volume 40% -> 50%" (read the current level first);
  steps by wording (agreed over a random 5-15): "slightly"/"a tiny bit" 5, "a bit"/"louder"/
  "quieter" 10, "a lot"/"way louder" 20, a number the user gives exactly (in the tool
  description so the model maps it). (2) Agreed: `mute_toggle` -> `set_mute(on/off)`,
  reports the state. (3) Agreed: media via Windows' media session API (GSMTC): real play vs
  pause, "what's playing" (app + title/artist), honest "nothing is playing".

- Clipboard (`get_`/`set_clipboard`, agreed 2026-09-30; both CONFIRMATION_REQUIRED since the
  original Phase 3 commit 7094884, 2026-08-23, "to protect private or sensitive data" -- never
  revisited): (1) `set_clipboard` LOW_RISK; `get_clipboard` runs when the user's message
  mentions the clipboard / "what I copied", asks when the model reaches for it unprompted.
  Side effect: every "open"/"copy"/"play" turn offers them today, and as guarded tools they
  force `_prefer_capable_slot` to main (log: "Guarded tools offered (get_clipboard, ...
  set_clipboard); routing to main") -- lowering them lets such turns stay on fast.
  (2) Copied files -> their paths; an image -> "image, WxH; paste it with Ctrl+V" (composer
  paste already attaches images). (3) Long content: length stated, start + "(N more chars)".

- `list_processes`/`kill_process` (agreed 2026-09-30): (1) **bug:** CPU% is always 0 --
  psutil's first `cpu_percent` reading per process is 0.0; prime, wait ~0.5 s, read again;
  sort by CPU when the question is about CPU. (2) Group like Task Manager's Apps view: one row
  per app ("chrome.exe x32, 2.1 GB, 4% CPU"), VRAM per app if the governor's source gives it
  cheaply. (3) `proc.terminate()` is TerminateProcess on Windows -- a hard kill, the
  "graceful" docstring is wrong: "close X" sends WM_CLOSE to its windows (the app can ask to
  save), force-kill only when the user says force / it's frozen, with the card saying unsaved
  work is lost. (4) Protect all of Jarvis's processes, not just the backend + parent:
  Jarvis.App, llama-server (chat) and the 8002/8003 sidecars, the uvicorn reloader.

- `send_toast` (agreed 2026-09-30): (1) replace with `remind_me(when, text)` -- stored on the
  routines scheduler (survives restarts), fires through `AwarenessMonitor.emit` (the client
  shows and speaks it) plus a Windows toast when Jarvis isn't the foreground window; list and
  cancel from chat. Covers part of the queued "routines from chat" missing tool. (2) Toasts do
  show on device (the user has seen them), but under the unregistered app_id "Jarvis
  Assistant": send under the app's AUMID so a click opens Jarvis.

- `get_system_status` (agreed 2026-09-30): (1) **bug, all tools:** `tool_schema.py:43` takes
  the docstring's first *line*, and this one wraps -- the model sees "...RAM, CPU, free disk,"
  and never "call this whenever..." / "the only hardware numbers you may quote". Take the first
  paragraph (up to a blank line / `Args:`), and audit every tool's description for the same
  cut. (2) Disk: every fixed drive with its letter (today `psutil.disk_usage(".")` = the
  backend's drive, D:, unlabelled). (3) Offer it on every turn (as web_search) -- "why is it
  laggy / fans loud / overheating" match no trigger today.

- `remember`/`forget` (agreed 2026-09-30, plus (a)-(d) below): (1) scope rule: anything said
  in a workspace about that workspace's work is saved to the workspace; only facts about the
  user as a person go global; the reply says where it was saved when it isn't obvious. The
  500-word rule (`7586e944`) was moved to the Prompt Enhancer workspace by hand
  (`project_preference`), as the user asked. (2) Remember/forget show as a visible chat line
  ("Remembered: ..." / "Forgot: ..." with Undo from the restorable bin), not just a collapsed
  tool row.

- Keyword tool offering (agreed 2026-09-30): measured all 31 schemas at ~15.4k chars / ~3.9k
  tokens (`remember` 1156 chars, `create_artifact` 1050 the largest). Replace
  `get_relevant_tools` with a **fixed set per space**, chosen at session start and never
  changed (byte-stable prefix from turn 1): Freeform = web, system status, apps/windows, audio,
  clipboard, processes, reminders, memory (~2k tokens); Workspace = that + files, git,
  artifacts, `execute_command` (~4-5k with the new tools). Routing: the **fast slot gets no
  tools** (chat and quick answers); anything that acts goes to main; `_prefer_capable_slot`'s
  "guarded tool offered" rule goes. Alternatives weighed: a small router call picking tool
  groups (extra latency, prefix shifts again) and a `find_tools` meta-tool (loads schemas
  mid-session, same prefix problem) -- both worse. Possible later add-on: per-workspace
  toggles for tool groups (a writing workspace without git/execute saves tokens for RAG).
  Mind the budget with §4.8a: 14-16k document + ~5k tools + history.

- Missing tools (agreed 2026-09-30): (1) **open a URL in the browser** -- `launch_app` accepts
  http(s) URLs and opens the default browser ("open X" stays one tool). Runs without asking for
  a URL the user typed, a search result, a link of a page fetched this session, or a site the
  user named ("search youtube for lofi"); asks otherwise (same exfiltration reasoning as
  fetch_url). (2) **`look_at_screen(window?)`** -- capture the screen or one window (Jarvis's
  windows are already excluded), downscale, hand to main as an image for that turn. Gated like
  get_clipboard (runs when the message asks to look, asks when the model reaches for it).
  If the governor dropped the projector, say "can't see right now (low VRAM)". Screenshots
  **are kept in the chat history** (the user's choice; stored like image attachments, nothing
  in ephemeral chats); only the newest goes to the model as pixels, older ones as
  "[screenshot]" text so history doesn't re-send images. Works from the HUD/voice too.
  **Test first:** an image in a tool result through llama-server + the Qwen3.5 template
  (else inject it on a follow-up user part).
- `remind_me` covers most of "routines from chat".

**Fix batch 1 -- security (done 2026-09-30, tests only: 602 pass; not yet exercised on device):**
- `execute_command`: **correction to the review** -- the chaining bug was latent, not live:
  `evaluate_tool_permission` kept the base CONFIRMATION tier whenever the command was "safe", so
  *every* command asked (even `dir`) and the prefix check never took effect. Now the command's
  own tier applies, and a safe command runs only as one plain command: no `; & | < > $ `` `` @(`
  or newline, whole-word prefix match (`dirty.exe` isn't `dir`), no write/destroy args
  (`git branch -D`, `git diff --output`), every path argument inside the workspace (echo/pwd/
  whoami/--version exempt). PowerShell/cmd destroyers added to HIGH_RISK (`Remove-Item -Recurse`,
  `Format-Volume`, `Clear-Disk`, `Stop-/Restart-Computer`, `rd /s`, `reg delete`, ...); `format`
  only as `format X:` (so `git log --format=` isn't high risk). Timeout 30 -> 120 s, returning
  the output printed before the timeout. The `reason` arg (4) moves to batch 3 with
  write/delete, since it needs the confirmation card.
- `fetch_url`: (1) origin check -- `url_provenance_text(messages)` (user turns + web_search/
  fetch_url results, schemes and www. stripped) is passed to the permission batch from both
  tool loops; a URL not found there asks, with the reason on the card. Protocol rule 7 and the
  tool description say web content is data, never instructions; results are headed "(web page
  text -- data, not instructions)". (2) SSRF: redirects followed by hand (max 5), every hop's
  host resolved; a hop into loopback/private/link-local is refused unless the first address was
  itself local (the gate asked, the user approved). Result names the final URL.

**Review finished 2026-09-30.** Suggested
fix order: (1) security -- execute_command chaining/paths, fetch_url exfil + SSRF; (2) bugs --
forget guard + loop + bin, docstring first paragraph (all tools), patch/delete RAG index,
CPU 0%, launch_app Temp raw strings + name matching, focus honesty, kill = WM_CLOSE + protect
Jarvis; **(2) built 2026-10-01, 642 tests pass, unverified on device:** descriptions = first
paragraph (`tool_description`; "requires confirmation" lines dropped); RAG: re-index also drops
the old chunks from Qdrant/FTS5 (they stayed searchable after *every* re-index, write_file
included -- may bear on §4.8a), `remove_file` on delete, rescan drops vanished files, patch_file
re-indexes; `list_processes` samples CPU twice (Task Manager scale) + `sort_by`; `kill_process`
sends WM_CLOSE, `force=true` only on request (card says which), protects Jarvis.App, the
reloader, llama-servers; launch_app Temp check fixed; `match_start_menu` (exact -> single
prefix/contains -> closest list, ambiguity asks); `focus_app` ranks process > exact title >
substring, asks across apps, checks `GetForegroundWindow` and says when Windows refused. Left
for later: the client calling `AllowSetForegroundWindow` (WinUI), Store/AppX apps, launch_app
focusing a running app; **(3) built 2026-10-01, 648 tests pass, unverified on device:** fixed
tool set per space (`tools_for_space`; keyword picking, `_offered_tools` and
`_prefer_capable_slot` gone); the fast model gets no tools and an action-looking message
(`looks_like_action`) never runs on it; file tools work outside the workspace once the gate
asked (`paths.py`, `allow_outside` injected only for gate-checked calls; the gate now sees
`list_directory`'s `directory_path`, which it missed); `write_file` replacing a file asks,
backups to `.jarvis/backups/` (newest 10 per file) on overwrite and patch; `delete_file` ->
Recycle Bin, folders too (card counts files); `reason` arg on write/patch/delete/execute shown
on the card, not part of the approval id; `set_clipboard` LOW_RISK, `get_clipboard` runs when
the message mentions the clipboard. Cost to watch on device: the first turn of a workspace chat
now prefills ~4k tokens of tool schemas even for "hi" (cached after). Not done from the per-tool
notes: list_directory sizes/caps, find_files/grep clutter rules, binary sniffing, PDF text,
fetch_url offset/links -- smaller behaviour items, still open in the notes above; **(4) built
2026-10-01, 671 tests pass, unverified on device:** git_checkout -> `git_switch` (create=true)
+ `git_restore` (backs up first); `git_push` (sets upstream on first push) and `git_pull
--ff-only` (diverged -> says so, touches nothing), both ask, GIT_TERMINAL_PROMPT=0; big diffs
show `--stat` first; `patch_artifact` (closest region on a miss, replace_all); `web_search
news=true` (date + source, newest first; description asks for the links used); `remind_me` /
`reminders` replace send_toast -- one-shot (`Routine.at`, fires late with a note if Jarvis was
off, then removed) or daily/weekdays/weekly, through `AwarenessMonitor.emit` plus a Windows
toast when Jarvis isn't in front, toasts under the app's AUMID (`JARVIS_AUMID` from
BackendHost -- that C# line is **not build-checked**, the app was running); `set_volume(change=)`
reports old -> new; `mute_toggle` -> `set_mute(on)`; `media_key` -> `media_control` on Windows'
media session API (new deps: winrt-* packages, in requirements.txt) with status / real play vs
pause, falling back to media keys. Also fixed protocol rule 9 (`list_directory(path=)` named a
parameter that doesn't exist). **(5) leftovers, built 2026-10-01, 689 tests pass, unverified on
device:** `launch_app` opens http(s) links in the default browser (runs for a link from the
user/search/opened page or on a site the user named, asks otherwise); the workspace's 20
newest artifacts listed in each turn's context, artifact tools scoped to the workspace,
`create_artifact`'s description carries the when-to-use rule; file tools: clutter-only skips
(`file_kinds.py`), binaries sniffed by content (read_file refuses with type+size, grep skips
them and files >5 MB), grep lines cut to ~200 chars with one line of context, find_files matches
folders and lists newest first with size/date, list_directory folders-first with size/date,
clutter folders collapsed, cap 200, read_file/patch_file say when they used a file found
elsewhere, patch_file shows the closest region on a miss and takes replace_all; fetch_url
`offset` + total length, links as a numbered list (max 30) at the end, honest JS-only and PDF
messages (no PDF reader installed -- pypdf would be used if added); get_system_status names
every fixed drive; list_processes groups per app (`group=false` for PIDs), idle process hidden;
`look_at_screen(window?)` -- tested first: llama-server + the Qwen3.5 template take an image
inside a tool result and the 9B read "7351" off a test image -- 1280 px max, saved to
`backend/data/screenshots` (temp for off-the-record), runs when the message asks to look, says
"can't see" when no projector is loaded; later turns get text only. A test (test_file_tools)
had been writing backups into the repo root -- moved to tmp. Batch 5 committed 308016a after the
user checked it on device. **(6) WinUI side, built 2026-10-01, app builds clean, 690 tests pass,
unverified on device:** collapsed tool rows say what they did ("Searched: ...", "Read: <url>",
"Looked at the screen"); look_at_screen's screenshot shows as a thumbnail under its row (click
opens it); remember/forget that changed something render as a "Remembered: ... / Forgot: ..."
line with Undo (`MemoryLineCard`; Undo on remembered -> new `POST /api/memories/{id}/undo-remember`,
which moves it to the bin; on forgotten -> the bin restore); reopened chats rebuild their tool
rows from the stored tool rows (they were dropped before), so screenshots and memory lines come
back; `/health` reports the backend `pid` and the client calls `AllowSetForegroundWindow` for it
when a turn starts (typed or voice) -- `ForegroundGrant`. Still open: PDF text in grep (needs
pypdf, not installed). (3) behaviour -- fixed tool sets + routing, read/write outside the workspace with
approval, backups, clipboard tiers; (4) new tools -- remind_me, git push/pull/switch/restore,
patch_artifact, news search, media session. Earlier notes:
- `launch_app`: **bug** -- `permissions.py` ~309 checks `"\temp\\"`, `"\tmp\\"`,
  `"\appdata\local\temp\\"` in plain strings: `\t` is a tab and `\a` a bell, so the Temp
  escalation never matches.
- `stop_playback` leftovers: `tools/audio_playback.py` is dead (nothing calls `play_audio`);
  only `main.py`'s shutdown call and the always-false `is_playing_audio` status field
  (unused `IsPlayingAudio` in `ApiModels.cs`) keep it alive.
- **Not added by claude** - I dont know if we have talked about this before but let me put about
  it again. Whenever i open a app like discord or chrome, if i give the name correctly it opens
  correctly, if not it doesnt open For example: if give like open qwen studio app. Refer open
  qwen studio chat happened on 2026-09-29. **bug** - for some reason the model forget the 
  prompt i gave in opus 5.5 prompt enhancer workspace (about the 500 length for a prompt)

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
Built 2026-09-27 (build clean; runs; the sidebar, toolbar, composer and inspector seen in two
snapshots). Unverified on device: the status popover (meters, Governor, Free VRAM), session
hover "..." and search, hiding the sidebar, the mic button, the ephemeral eye, tool-step rows,
inspector auto-open, window drag and caption buttons. The transcript's text fades under the
popover (glass can't frost XAML). Known: a SystemBackdrop ArgumentException is logged (caught)
when the appearance changes. **Confirmed on device by the user 2026-10-01 ("worked great"),
committed 2242c90** together with the model choice below.

**Model choice (2026-09-28, the user: "works good" on device):** the status popover's "Answer with" switch --
Auto | Main | Fast -- sent as the turn's `model` (`ChatViewModel.ModelChoice`), saved in
LocalSettings. Auto is today's routing; Main/Fast pin that slot (the backend's explicit model
skips `_prefer_capable_slot`/`_prefer_loaded_slot`; images still go to main). Asked for because
the fast model never ran: nothing in the app requested it. The HUD/voice path still sends no
model (Auto). Not specifically checked: Fast turns with a guarded tool. Uncommitted, together
with the AppLayout work.

**d. Traffic-light window buttons (asked by the user 2026-09-30, after the tool fixes):**
replace the system caption buttons (top right, `CaptionButtonsWidth` 138 in
`MainWindow.xaml.cs`) with macOS-style round buttons -- red close, yellow minimize, green
maximize -- from the user's reference image. Needs: hide the system buttons
(`OverlappedPresenter.SetBorderAndTitleBar(true, false)`), our own buttons outside the drag
region (`SetRegionRects`), and keep Windows 11 Snap Layouts on hover over green (answer
`WM_NCHITTEST` with `HTMAXBUTTON` for its rect) or it's lost. Colours as palette tokens with
dark/light/HC variants. Decided by the user: **top right** where the system buttons are now,
order yellow, green, red (close stays in the corner); **glyphs only on hover** (plain dots
otherwise, as macOS); **green = maximize** (Snap Layouts kept); **main window only** (the HUD
stays borderless). Size: match macOS proportions scaled to our caption row -- check with a
snapshot.

**b. Still open:** adaptive glass (renderer: per-slab luminance, flip small chrome between
dark/light, raise tint on reading areas -- thresholds in the artifact); app tile PNGs from
`Assets/Brand/jarvis-app-tile.svg` (manifest assets are still the template cross); awareness
toasts as glass (they are XAML borders, and a sibling slab would share the messages layer).

### 4.8 After the tool review

**a. Hallucinated names in RAG answers (raised by the user 2026-09-30).** The user's reference
document is ~11,000 words (~14-16k tokens) and growing -- it fits the 32k context today, not
forever. The model gets names wrong even when answering from retrieved chunks, and correcting it
in chat doesn't stick.
- **Step 1 -- diagnostic first (current step):** log the exact retrieved chunks for a failing
  query and check whether the correct name is in them. Build a small test set of name-lookup
  queries (question + expected name) to run against it.
  - Name **not** in the chunks -> retrieval problem: chunk size/overlap, embedding model, top-k;
    consider hybrid BM25 + embedding search (names are a classic miss for pure embeddings).
  - Name **in** the chunks -> generation problem, go to step 2.
  - **Built 2026-10-01 (696 tests pass; not yet run on the real index):** every WORKSPACE turn
    (not off-the-record) appends a trace to `backend/data/rag_trace.jsonl` (`app/rag/trace.py`):
    semantic and keyword top 20, reranked scores, the floor / "flat relevance" stop, final,
    the full text sent to the model, the answer and `names_not_in_passages` (capitalised words
    in the answer no passage contains). `POST /api/rag/trace {project_id, query, expect}` runs
    the same retrieval without the model and names the stage where `expect` was lost.
    `scripts/rag_name_check.py --project "Game development" --tests data/rag_name_tests.json
    [--ask]` runs a test set through it (`--ask` also asks the model, off the record). The test
    set (15 questions drafted from `game_design_document.md`, 976 words -- the user: part of the
    real document, and it hallucinates on this already) is waiting for the user's check.
    Noticed while reading: the hybrid keyword (FTS5) search already exists, so "add BM25" is
    done; the reranker returns *nothing* when the top six score alike ("flat relevance") -- a
    suspect for name questions; the document calls the Entity both "his" and "her".
  - **First run (user, 2026-10-01, `backend/data/rag_checks/20261001-112309.json`):** 13/15
    answers right, retrieval delivered 14/15. Q10 "World Tree a reference to?" -> both searches
    ranked the right two passages first (BM25 19 vs 7 next) but their cosines (0.599/0.584) sat
    under the 0.62 floor (measured on code) -> nothing sent -> "Yggdrasil". Q11 "weather in
    Region 1" -> passage delivered, model refused it as a real forecast. Right answers still
    embellished (Q14 invented a link; Q5 "your world beings"). Also: RRF dropped the keyword
    score of a chunk both searches found.
  - **Fixes chosen by the user (1 + 3), built 2026-10-01, 701 tests pass, unverified on
    device:** (1) keyword standouts (`RerankerService.keyword_standouts`: within 60% of the best
    BM25, best >= 1.5x the next hit, at most 3) pass the floor, the flat-relevance stop and
    top_k; RRF keeps both searches' fields. The 0.62 floor itself stays (lowering it lets
    unrelated code into the Jarvis workspace). (3) the passages block opens with
    `WORKSPACE_PASSAGES_RULE` (quote the supporting line first, add nothing, "the files don't
    say", named things are the project's); a workspace question that found nothing gets
    `NO_PASSAGES_NOTE` as a turn directive. **Re-run on device 2026-10-01
    (`20261001-113048.json`): 15/15**, Q10 answers "AOT Tree reference", answers now open with a
    quote (some quotes still lightly paraphrased; Q3 added "in the cave"). Next: the user's real
    failing questions in chat (they are traced); whole-document mode (2) kept for the full
    ~11k-word document.
- **Step 2 -- generation-side fixes, in order:** quote the supporting sentence first, then
  answer; a code check that the answer literally appears in the retrieved chunks (reject/retry
  if not); temperature 0-0.2 and thinking off for lookups; "not found in the provided text" as
  a valid answer; fewer, cleaner chunks (3-5).
- **Step 3 -- quant test:** Q4_K_M vs the current `UD-IQ3_XXS` with identical chunks, prompt and
  temperature 0; measure real VRAM with nvidia-smi.
- **Process (the user's terms):** design the diagnostic (logging + test set) first; no fix spec
  until the user has run it and reported results; ask about anything ambiguous, don't assume.

**b. Model download from HuggingFace + hardware suggestions.** Download GGUFs from inside the
app; suggest models from the detected RAM / VRAM / CPU.

**c. Then: evaluate DavidAU's "Defiant Fable" 9B as the main model** (raised by the user
2026-09-28): https://huggingface.co/DavidAU/Qwen3.5-9B-The-Defiant-Fable-Uncensored-Heretic-NEO-IMATRIX-MAX-MTP-GGUF
-- a Qwen3.5-9B multi-fine-tune merge, "Heretic" (refusals removed), same architecture, so it
runs on our llama-server as is.
- **Its files don't fit the budget as shipped:** every quant keeps the output tensor at 16-bit
  (~2 GB on Qwen3.5's vocab). Smallest IQ2_M 4713 MiB, IQ3_M 5355 MiB, Q4_K_S 6248 MiB, vs our
  `UD-IQ3_XXS` 3830 MiB (whole main stack ~5.0-5.2 GB). IQ3_M would put us at ~6.6 GB.
- **Plan:** download the regular (non-MTP) Q8_0 (9996 MiB), build an imatrix from it with
  `tools/llama-cpp/llama-imatrix.exe`, requantize to IQ3_XXS with a normal-size output tensor
  (`llama-quantize.exe --imatrix ...`), target ~3.8 GB. Then A/B against the current main
  in both orders (thermal drift): PPL on repo prose, `scripts/bench_tool_chain.py` (current
  5/5), decode speed. Replace only if tool chains hold.
- Skip the MTP files (MTP tensors add ~100-130 MiB; llama.cpp build support unverified) and the
  "plusIQ" variants (default xhigh reasoning, in-chat `{REASON:...}` switches that persist and a
  changed template -- risk to the byte-stable prompt prefix; Q6/Q8 only). A "plusIQ-TOOLS"
  variant exists, also Q6/Q8 only -- worth a look if it appears in small quants.
- Its ARC/HellaSwag-style benchmarks say nothing about tool calling; the merge + abliteration
  may hurt it. Uncensored is fine for us: risky tools are gated by `permissions.py`, not by
  model refusals.
- Vision: try our existing `mmproj-Q8_0.gguf` (the fine-tune likely left the vision tower
  alone -- unverified); their mmproj is F16 876 MiB, else quantize it with
  `scripts/quantize_mmproj.py`.

**d. Developer settings: Liquid Glass sliders** -- GlassLab's material sliders in the app's
Settings for live tuning.

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
