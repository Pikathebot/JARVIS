# Jarvis — REST and Streaming API Reference

> Current as of 2026-10-25, derived from the routers in `backend/app/`. The request and response shapes are the
> Pydantic models in those files (`routers/*.py`, `main.py`), which are the source of truth; this page lists the
> endpoints and what they are for. `GET /docs` on a running backend shows the live schema.

The backend runs on `http://127.0.0.1:8000`. The phone and iPad reach the same app on port 8005, loopback only,
through `tailscale serve`; `routers/remote.py` marks everything arriving there as remote.

---

## 1. Chat and streaming

| Method | Path | Purpose |
|---|---|---|
| POST | `/chat/stream` | The main endpoint. Server-sent events for one turn (below). |
| POST | `/chat` | The same turn without streaming; returns the final reply. |
| GET | `/sessions` | List sessions, optionally `?chat_mode=FREEFORM` or `WORKSPACE`. |
| GET | `/sessions/{id}/messages` | Messages of a session. |
| GET | `/sessions/{id}/compactions` | Compaction records for a long session. |
| DELETE | `/sessions/{id}` | Delete a session. For an ephemeral session this also purges its in-memory history. |
| POST | `/api/voice/command` | A spoken or typed command, routed by the voice command brain (`routers/voice.py`). |

Request fields for `/chat/stream` include `message`, `session_id`, `project_id`, `chat_mode` (`FREEFORM` or
`WORKSPACE`), `ephemeral`, `attachments` and `approved_action_ids`. Approval of a pending action is sent as its
`act_<hash>` token in `approved_action_ids`.

### Server-sent events

Events the client handles, from `agent/orchestrator.py` and `llamacpp_provider.py`:

- `token` / `text_delta`: a piece of the reply.
- `reasoning`: a piece of the model's thinking, shown in the Reasoning tab, not in the transcript.
- `tool_draft` / `tool_call`: a tool call as the model wrote it, and as it will run.
- `tool_start` / `tool_end`: a tool running, and its result (truncated when long).
- `retrieval_context`: the RAG chunks used for this turn (Workspace only).
- `confirmation_required`: an action card to answer. **The stream ends here without `done`.** Answer through
  section 4, then send the turn again with the token in `approved_action_ids`.
- `done`: the turn is complete. May carry `replace_content` to swap the bubble text (used for fixed fast-model replies).
- `error`: the turn failed.

---

## 2. Models and governor

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/models` | The models served right now, per slot. This is the runtime truth. |
| POST | `/api/models/select` | Choose the model for a slot (writes `data/models.json`). |
| DELETE | `/api/models/select/{slot}` | Clear a slot's selection. |
| POST | `/models/load`, `/models/unload`, `/models/warm` | Load, free or warm the model. |
| GET | `/api/models/hub/curated`, `/hub/search`, `/hub/files` | Browse the model catalogue and Hugging Face. |
| POST | `/api/models/download` | Download a model file (job). |
| GET / DELETE | `/api/models/downloads`, `/downloads/{job_id}` | Download jobs. |
| GET | `/governor/status` | VRAM, GPU and CPU telemetry and the governor's state. |
| GET | `/governor/history` | Recent governor samples. |
| POST | `/governor/pause`, `/resume`, `/resume-override`, `/auto` | Control the governor. |
| POST | `/governor/force-reload`, `/clear-error` | Reload the model; clear a launch error. |
| GET / POST | `/api/game-mode` | Read or set game mode (unload everything before a game). |
| GET | `/health` | Liveness and the summary the client shows. |
| GET | `/api/logs` | Log tails for Settings → Developer. |

---

## 3. Projects, files, artifacts and memory

| Method | Path | Purpose |
|---|---|---|
| GET / POST | `/api/projects` | List or create projects. |
| GET / PUT / DELETE | `/api/projects/{id}` | Read, update or delete a project. |
| POST | `/api/projects/{id}/activate` | Make it the active project. |
| POST | `/api/projects/{id}/index` | Re-index its files for RAG. |
| GET / PUT | `/api/projects/{id}/state` | `PROJECT.md` living state. |
| GET | `/api/projects/{id}/files` | Files in the project folder. |
| GET | `/api/projects/active/current` | The active project. |
| POST | `/api/upload` | Attach a file to a session (stored, indexed when it is text). |
| GET / DELETE | `/api/attachments`, `/attachments/{id}` | Attachments. |
| GET / POST | `/api/artifacts` | List or create durable artifacts. |
| GET / PATCH / PUT / DELETE | `/api/artifacts/{id}` | Read or change an artifact. |
| GET / POST | `/api/artifacts/{id}/versions` | Version history. |
| GET | `/api/artifacts/{id}/versions/{version}` | One version. |
| POST | `/api/artifacts/{id}/restore/{version}` | Restore a version. |
| GET / POST | `/api/memories` | Memories (`GET` lists, `POST` adds). |
| GET / PATCH / DELETE | `/api/memories/{id}` | One memory. |
| GET | `/api/memories/bin` | Deleted memories; `POST .../bin/{id}/restore` brings one back. |
| POST | `/api/memories/detect`, `/deduplicate`, `/{id}/undo-remember` | Memory housekeeping. |
| POST | `/api/rag/trace` | Debug trace of a RAG query. |

---

## 4. Confirmations, persona, routines and awareness

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/confirmations` | Pending action cards. |
| POST | `/api/confirmations/{action_id}/deny` | Deny a pending action. Approval is sent with the chat turn. |
| GET / POST | `/api/persona`, `PATCH` / `DELETE` `/api/persona/overrides` | The manner and its overrides. |
| POST | `/api/persona/speech-preview` | Speak a sample line. |
| GET / POST | `/api/routines`, `PATCH` / `DELETE` `/api/routines/{id}`, `POST .../{id}/run` | Scheduled routines. |
| GET | `/api/awareness/status`, `/observations`, `/briefing`, `/config` | Awareness state and the briefing. |
| POST | `/api/awareness/poll`, `/observations/{id}/ack` | Poll now; acknowledge an observation. |
| PATCH | `/api/awareness/config` | Change awareness settings. |
| GET | `/api/awareness/stream` | SSE stream of unprompted observations. |
| GET | `/skills` | Loaded skills. |
| POST | `/skills/reload` | Reload `skills/*.md` from disk. |
| GET | `/mcp/servers` | Configured MCP servers. |

---

## 5. Freeform thread

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/thread/head` | The current thread head. |
| GET | `/api/thread/live` | The live thread state for the client. |
| GET | `/api/thread/messages` | Messages of the thread. |
| GET | `/api/thread/episodes` | Episode records (earlier topics). |
| DELETE | `/api/thread/episodes/{id}` | Forget an episode. |

---

## 6. Voice

| Method | Path | Purpose |
|---|---|---|
| POST | `/voice/transcribe`, `/api/voice/listen` | Speech to text for one utterance. |
| POST | `/voice/speak`, `/voice/tts`, `/api/voice/say` | Text to speech. |
| GET | `/voice/voices`, `/voice/status` | Voices and voice state. |
| GET / POST | `/api/voice/mode` | Read or set voice mode (starts Laya on the GPU). |
| GET | `/api/voice/hands-free` | Hands-free state. |
| GET / POST | `/api/voice/session/{id}`, `/start`, `/stop`, `/state`, `/arm` | A voice session. |

---

## 7. Remote (phone and iPad)

Served on port 8005 and reached through Tailscale. Everything arriving there is remote.

| Method | Path | Purpose |
|---|---|---|
| GET / POST | `/api/remote/pairing` | Pairing code. |
| POST | `/api/remote/pair` | Pair a device. |
| GET / DELETE | `/api/remote/devices`, `/devices/{id}` | Paired devices. |
| GET | `/api/remote/status`, `/api/remote/me` | Remote status and the current device. |
| GET | `/api/remote/push/key` | Web push key. |
| POST / DELETE | `/api/remote/push` | Subscribe or unsubscribe for push. |
| POST | `/api/remote/push/test` | Send a test notification. |
| POST | `/api/remote/transcribe`, `/say` | Voice from the device. |

---

## 8. Google

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/google/status` | Signed-in state. |
| POST | `/api/google/sign-in`, `/sign-out` | Sign in or out. |
| POST | `/api/google/sync` | Sync Tasks and Calendar now. |

Setup for Google is in `docs/GOOGLE_SETUP.md`.

---

## 9. Compare (model A/B reviews)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/compare/{id}` | One comparison. |
| POST | `/api/compare/{id}/vote` | Record a blind vote. |
