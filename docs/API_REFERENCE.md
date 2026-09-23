> **Historical document.** Written before the WinUI client, the current models and the
> 2026-09 clean-ups; parts of it no longer describe the system. For how Jarvis works today
> see `CLAUDE.md` and `PLAN.md` at the repo root.

# Jarvis Assistant — REST & Streaming API Reference

The Jarvis backend runs on `http://127.0.0.1:8000`.

---

## 1. Chat & Inference

### `POST /chat/stream` (Primary SSE Endpoint)
Real-time Server-Sent Events (SSE) streaming endpoint emitting token deltas, tool status events, and completion metadata.

**Request Body (`application/json`):**
```json
{
  "message": "Analyze the attached file and summarize the logic.",
  "session_id": "session_12345",
  "project_id": "proj_abcde",
  "model": "qwen3.5:9b",
  "mode": "auto",
  "chat_mode": "WORKSPACE",
  "system_prompt": null,
  "approved_action_ids": ["act_1a2b3c4d5e6f"],
  "attachments": [
    {
      "id": "att_9876",
      "filename": "calc.py",
      "path": "d:/JARVIS/workspace/projects/proj_abcde/files/calc.py"
    }
  ]
}
```

**SSE Events Emitted:**
- `event: token` &rarr; `{"delta": "The script defines..."}`
- `event: tool_start` &rarr; `{"tool": "read_file", "args": {"file_path": "calc.py"}}`
- `event: tool_done` &rarr; `{"tool": "read_file", "output": "..."}`
- `event: confirmation_required` &rarr; `{"pending_confirmations": [{"action_id": "act_...", "tool": "write_file", "args": {...}}]}`
- `event: done` &rarr; `{"response": "...", "model": "qwen3.5:9b", "tools_used": [...]}`

---

### `POST /chat`
Synchronous JSON chat endpoint. Returns full response payload upon turn completion.

---

### `POST /models/unload`
Instantly halts `llama-server.exe` via `RuntimeProcessManager.stop()`, freeing 100% of GPU VRAM.

**Response (`200 OK`):**
```json
{
  "status": "ok",
  "message": "Local llama.cpp runtime process stopped and VRAM released."
}
```

---

## 2. Projects & Workspaces (`/api/projects`)

### `GET /api/projects`
List all user project workspaces.

### `POST /api/projects`
Create a new workspace and generate Section 7 directory structure (`files/`, `knowledge/`, `artifacts/`, `memory/`, `indexes/`).

**Request Body:**
```json
{
  "name": "My AI Project",
  "description": "Optional project description",
  "system_instructions": "Custom system prompt override",
  "local_folders": ["C:/Source/App"]
}
```

### `GET /api/projects/{project_id}`
Retrieve project workspace details.

### `PUT /api/projects/{project_id}`
Update project details, custom instructions, or local folder paths.

### `DELETE /api/projects/{project_id}`
Delete a project workspace record.

### `GET /api/projects/{project_id}/files`
List all files stored in the project's `files/` directory.

---

## 3. Attachments & File Uploads (`/api/upload`)

### `POST /api/upload`
Upload a file attachment with strict security checks (extension whitelist, max 50MB, filename traversal sanitization).

**Form Data (`multipart/form-data`):**
- `file`: Binary file upload
- `project_id`: Optional project identifier
- `session_id`: Optional session identifier

**Response (`201 Created`):**
```json
{
  "id": "att_12345",
  "project_id": "proj_abcde",
  "session_id": "session_12345",
  "filename": "data.csv",
  "path": "d:/JARVIS/workspace/projects/proj_abcde/files/data.csv",
  "size_bytes": 1024,
  "content_type": "text/csv",
  "created_at": "2026-08-31T12:00:00Z"
}
```

---

## 4. Artifacts & Versioning (`/api/artifacts`)

### `GET /api/artifacts`
List AI-generated artifacts. Supports filtering by `?project_id=...` or `?session_id=...`.

### `POST /api/artifacts`
Create a new AI-generated artifact and persist to disk.

### `GET /api/artifacts/{artifact_id}`
Retrieve artifact details.

### `PUT /api/artifacts/{artifact_id}`
Update an artifact and automatically snapshot a new version.

### `GET /api/artifacts/{artifact_id}/versions`
Retrieve the complete version history for an artifact.

---

## 5. System Health & Hardware Governor

### `GET /health`
Returns backend connectivity, primary model status, and governor health.

### `GET /governor/status`
Returns real-time PyNVML GPU/VRAM and psutil CPU/RAM metrics.

---

## 6. Sessions & Memory

### `GET /sessions`
List conversation sessions (supports filtering via `?project_id=...`).

### `GET /sessions/{session_id}/messages`
Retrieve ordered message turns for a conversation session.

### `DELETE /sessions/{session_id}`
Delete a conversation session and all its messages.
