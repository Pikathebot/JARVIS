# Jarvis Assistant — User Guide

Welcome to Jarvis, your private, lightweight local AI assistant for Windows 11.

---

## 1. Quick Start

### Launching Jarvis
- **From Windows**: Double-click `Start Jarvis.bat` in the repository root. It builds the native client on first run, registers it, launches it, and the app starts the backend itself. Pass `-Build` to force a rebuild after pulling changes.

---

## 2. Desktop Interface Overview

The Jarvis desktop application is a native WinUI 3 client ([`desktop-winui/`](../desktop-winui/)) with 3 integrated areas:

### 1. Left Sidebar
- **Workspace Switcher**: Select between your isolated Project Workspaces or switch to Global chat.
- **New Project Modal**: Create a new project with custom system instructions and designated local folders.
- **Session History**: Easily switch between previous conversation sessions or launch a new chat.

### 2. Main Conversation Viewport & Composer
- **Live SSE Streaming**: Fluid, real-time message streaming with tool execution logs.
- **Paperclip Attachment Button**: Upload source code, markdown, PDFs, or data files. Files are automatically processed and injected into the LLM context.
- **Deterministic Action Cards**: Approve or deny sensitive operations (`write_file`, `execute_command`) with one click.

### 3. Right Panel
- **Artifacts**: View and browse AI-generated durable code snippets, markdown reports, and version history.
- **Files**: Inspect all files stored in the active project workspace.
- **Context**: View active system instructions and context usage.
- **Activity**: Inspect hardware telemetry and background tool execution logs.
- **Reasoning**: The model's thinking for the current reply, kept out of the transcript. It only shows a badge; it never opens itself.

---

## 3. Voice Interaction & Wake-Word

Jarvis includes hands-free voice interaction:
1. **Say the Wake-Word**: Say **"Jarvis, ..."** or **"Hey Jarvis, ..."** followed by your query.
2. **Conversational Speech Controls**:
   - Say `"Voice off"` or `"Be quiet"` to mute vocal responses.
   - Say `"Voice on"` to re-enable audio output.

---

## 4. Safety Permissions & Action Confirmations

When an operation touches the filesystem or executes system commands, Jarvis evaluates deterministic risk tiers:
- **Low-Risk Tools** (`read_file`, `web_search`, `fetch_url`): Executed immediately.
- **Confirmation-Required Tools** (`write_file`, `patch_file`, `app_control`): Jarvis presents an **Action Confirmation Card**:
  - Click **`Approve & Execute`** to authorize the action with a unique cryptographic action token.
  - Click **`Cancel`** to reject.

---

## 5. Adding Custom Skills

You can extend Jarvis with custom skills without writing Python code:
1. Create a `.md` file in the `skills/` directory (e.g. `skills/git_helper.md`):
   ```markdown
   ---
   name: git_helper
   description: Specialized git assistance and repository management
   triggers:
     - git status
     - commit changes
     - git branch
   tools:
     - execute_command
   ---

   # Git Assistant Instructions
   - Always run git status before staging changes.
   - Format commit messages cleanly following Conventional Commits.
   ```
2. Reload skills dynamically by calling `POST http://127.0.0.1:8000/skills/reload`.
