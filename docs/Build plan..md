> **Historical document.** Written before the WinUI client, the current models and the
> 2026-09 clean-ups; parts of it no longer describe the system. For how Jarvis works today
> see `CLAUDE.md` and `PLAN.md` at the repo root.

# Build a Local Claude-Like AI Workspace

You are building a fully local AI assistant application inspired by the architecture and workflow of modern Claude-style products.

The objective is NOT to copy proprietary code or branding. Reproduce the useful product architecture locally:

* persistent chat
* projects/workspaces
* project knowledge
* RAG
* long-term memory
* contextual retrieval
* artifacts
* file manipulation
* terminal/tool execution
* agentic loops
* skills
* model/tool routing
* streaming
* local execution
* safe sandboxing
* modern chat UI

Everything should be designed to work locally and be usable with OpenAI-compatible local inference servers.

## Primary design principle

Do NOT turn the LLM into the entire application through a huge system prompt.

The application must provide intelligence through orchestration:

LLM

* context manager
* retrieval
* memory
* tools
* skills
* workspace
* agent loop
* artifact system.

The LLM should be treated as one component of the system.

---

# 1. Required architecture

Use a modular architecture:

Frontend:

* React
* Next.js
* TypeScript
* Tailwind CSS
* component-based UI
* streaming responses
* responsive desktop-first layout

Backend:

* Python
* FastAPI
* async architecture
* WebSocket or SSE streaming

Persistence:

* SQLite initially
* SQLAlchemy or SQLModel
* migrations
* structure code so PostgreSQL can later replace SQLite

Vector storage:

* local vector database
* prefer Qdrant local mode or a similarly lightweight local solution
* must support metadata filtering

Embeddings:

* Qwen3-Embedding-0.6B

Reranking:

* Qwen3-Reranker-0.6B

LLM:

* configurable OpenAI-compatible endpoint
* default model should be configurable
* support LM Studio
* support llama.cpp
* support Ollama
* support vLLM

Do NOT hard-code the application to one inference backend.

---

# 2. Model abstraction layer

Create a provider abstraction:

ModelProvider

Required operations:

* chat()
* stream_chat()
* generate()
* count_tokens()
* embeddings()
* rerank()
* model_info()

Implement an OpenAI-compatible provider first.

Configuration must allow:

MAIN_MODEL
FAST_MODEL
VISION_MODEL
EMBEDDING_MODEL
RERANKER_MODEL

Example:

MAIN_MODEL=Qwen3-30B-A3B
FAST_MODEL=Qwen3.8-9B-Distill
EMBEDDING_MODEL=Qwen3-Embedding-0.6B
RERANKER_MODEL=Qwen3-Reranker-0.6B

The user must be able to change models from Settings without changing application code.

---

# 3. Conversation system

Implement persistent conversations.

Database entities:

User
Project
Conversation
Message
Attachment
Artifact
Memory
ToolCall
ToolResult
Skill
Workspace
Document
DocumentChunk
Task
AgentRun

A conversation contains:

* system configuration
* project association
* messages
* attachments
* tool calls
* artifact references
* metadata
* timestamps

Support:

* create conversation
* rename conversation
* delete conversation
* archive conversation
* search conversations
* regenerate response
* edit previous user message
* branch conversation
* continue from branch

Do not store only the final text.

Persist structured tool calls and results separately.

---

# 4. Claude-style context management

This is one of the most important components.

Create:

ContextManager

It must decide what information should be placed into the model context.

Inputs:

* current user message
* recent conversation messages
* project information
* project instructions
* retrieved documents
* relevant memories
* relevant previous conversations
* tool results
* active artifacts
* active skills
* system instructions

Output:

A prioritized context package.

Never blindly send all available project data to the model.

Context priorities:

1. current user request
2. current task/tool state
3. immediately relevant files
4. recent conversation
5. project instructions
6. relevant memories
7. retrieved historical context
8. low-priority background information

Implement token budgeting.

Example:

MAX_CONTEXT_TOKENS=28000

The context manager must reserve room for:

* reasoning
* tool calls
* response
* future tool results

Do not fill the entire context window with retrieved documents.

---

# 5. Conversation compression

Implement automatic summarization when a conversation grows too large.

Maintain:

Recent messages
+
rolling summary
+
important facts
+
open tasks
+
artifacts
+
tool state

Older messages should not simply disappear.

Create a structured summary such as:

conversation_summary:
important_facts:
decisions:
open_tasks:
files_modified:
artifacts_created:
tool_state:

The summarization system must be model-independent.

---

# 6. Long-term memory

Implement a local memory subsystem.

Memory categories:

* user preferences
* project preferences
* recurring facts
* workflow preferences
* important decisions
* persistent task context

Do NOT save every conversation sentence as memory.

Create a MemoryManager with:

detect_candidate_memories()
store_memory()
retrieve_memories()
update_memory()
delete_memory()
deduplicate_memories()

Memory retrieval should be semantic.

Allow users to inspect, edit and delete memories.

---

# 7. Projects / workspaces

Projects are persistent working environments.

Each project contains:

* project name
* description
* instructions
* local folders
* knowledge sources
* memory
* conversations
* artifacts
* skills
* tool permissions

Filesystem layout:

workspace/
projects/
project-id/
project.json
instructions.md
files/
knowledge/
artifacts/
memory/
indexes/

Allow a user to add local folders to a project.

The application must NOT copy the entire folder into the prompt.

Instead:

index -> retrieve -> rerank -> context.

---

# 8. Codebase-aware indexing

The system must support source-code projects.

Index:

* .cpp
* .h
* .hpp
* .cs
* .py
* .js
* .ts
* .tsx
* .json
* .yaml
* .yml
* .ini
* .md
* .txt
* .uproject
* .uasset metadata when possible
* relevant Unreal Engine project files

Create semantic chunks.

Also preserve:

* file path
* symbol
* function
* class
* namespace
* line numbers
* imports/includes
* language
* git commit
* modified timestamp

Do not use naive fixed-size chunking exclusively.

Use syntax-aware chunking whenever possible.

For code retrieval:

query
-> semantic retrieval
-> lexical/path retrieval
-> reranker
-> context selection.

---

# 9. Retrieval pipeline

Implement hybrid retrieval.

Pipeline:

User query
↓
query analysis
↓
vector retrieval
↓
keyword/path retrieval
↓
merge candidates
↓
Qwen3-Reranker-0.6B
↓
deduplicate
↓
context budget selection
↓
LLM

Support retrieval metadata filtering.

Examples:

project_id
file_type
language
path
symbol
git_branch

The UI should optionally expose:

"Retrieved 8 relevant files"

so the user can see what the agent used.

---

# 10. Agent runtime

Create a real agent loop.

Conceptually:

while task_not_complete:

```
build_context()

model_response = model()

if tool_call:
    execute_tool()
    save_tool_result()
    continue

if file_change:
    apply_change()
    continue

if task_complete:
    return_final_response()
```

Do NOT fake this through prompt instructions.

Tool calls must be structured.

Agent state must be persisted.

AgentRun should track:

* run id
* conversation id
* current step
* model
* context
* tool calls
* results
* files changed
* errors
* elapsed time
* final status

---

# 11. Tool system

Implement a ToolRegistry.

Every tool needs:

* name
* description
* JSON schema
* permission level
* execution function
* timeout
* cancellation support

Initial tools:

filesystem.read
filesystem.write
filesystem.edit
filesystem.create_directory
filesystem.delete

terminal.execute

git.status
git.diff
git.log
git.checkout
git.commit

search.project
search.conversation
search.memory

artifact.create
artifact.update
artifact.read

python.execute

Important:

Never give the model unrestricted host-machine access.

Use permission checks.

---

# 12. Terminal sandbox

Implement safe execution.

The first version can execute through a configurable sandbox abstraction.

Architecture:

Tool
↓
SandboxManager
↓
Execution backend

Backends should be pluggable:

* local restricted process
* Docker
* Windows Sandbox
* VM

Every execution must have:

* timeout
* working directory
* environment restrictions
* stdout
* stderr
* exit code
* cancellation

Never allow an arbitrary model-generated command to silently execute with unrestricted privileges.

Require explicit permission configuration for dangerous operations.

---

# 13. Skills system

Implement a skill system inspired by dynamically loaded agent capabilities.

Directory:

skills/
unreal-engine/
skill.md
scripts/
resources/

```
python/
    skill.md
    scripts/

git/
    skill.md
    scripts/

research/
    skill.md
```

Skill metadata:

name
description
triggers
instructions
allowed_tools

Do not load every skill into context.

Use progressive disclosure:

discover skill
↓
load metadata
↓
determine relevance
↓
load detailed instructions
↓
execute

---

# 14. Unreal Engine skill

Create a first-class Unreal Engine skill.

It should understand:

* .uproject
* Source/
* Plugins/
* Config/
* Content/
* Build.cs
* Target.cs
* C++
* reflection macros
* UCLASS
* UPROPERTY
* UFUNCTION
* replication
* Gameplay Ability System
* Blueprints
* common Unreal build workflows

Tools should eventually support:

unreal.detect_project
unreal.read_logs
unreal.build
unreal.run_editor
unreal.run_tests

Do not assume a fixed Unreal installation path.

Detect it from configuration.

---

# 15. Artifacts

Implement an Artifact system.

Artifacts are durable outputs separate from chat text.

Examples:

* code
* HTML
* CSS
* JavaScript
* Markdown
* JSON
* CSV
* Python
* SVG
* documents

Artifact entity:

id
conversation_id
project_id
name
type
content
version
created_at
updated_at

Support version history.

UI:

Chat
|
+---- Artifact panel

Artifact panel should support:

* source view
* preview
* versions
* download
* edit
* regenerate
* open in workspace

For HTML/React/etc., preview using a sandboxed environment.

---

# 16. File editing

Do not let the model rewrite whole files unnecessarily.

Implement:

read_file
apply_patch
replace_range
insert
delete
create_file

Use diff-based edits where possible.

Before changing a file:

* capture previous version
* generate patch
* validate patch
* apply patch
* save version

UI should show:

Changed files

* diff
* accept/reject

---

# 17. Browser/search abstraction

Create a SearchProvider abstraction.

Do not force the whole system to depend on one web provider.

Interface:

search()
open()
extract()
crawl()

The system must work entirely locally for local tasks.

Web access should be an optional capability.

---

# 18. Multimodal architecture

Support images as attachments.

Do not make vision mandatory for normal text tasks.

Architecture:

image
↓
VisionProvider
↓
structured visual information
↓
context manager
↓
main LLM

Make VISION_MODEL configurable.

This allows a smaller local VLM to handle:

* screenshots
* diagrams
* Unreal Editor screenshots
* satellite imagery
* UI analysis

without forcing the primary model to process every image.

---

# 19. UI

Create a polished Claude-like desktop web interface.

Layout:

Left sidebar:

* New Chat
* Search
* Projects
* Chats
* Settings

Center:

* conversation
* streaming response
* tool activity
* attachments

Right panel:

* Artifact
* Files
* Retrieved context
* Agent activity

Composer:

[ + ] [attachment] [model] [skills] [tools] [message box] [send]

The UI should feel like a professional AI workspace rather than an admin dashboard.

Do not copy Anthropic branding, logos, colors or proprietary visual assets.

---

# 20. Tool activity UI

During agent execution show:

Planning
Searching project
Reading files
Editing files
Running command
Testing
Completed

Each step should be expandable.

Example:

✓ searched project
✓ found 6 relevant files
✓ read InventoryComponent.cpp
→ editing InventoryComponent.cpp
→ running build
✓ build succeeded

This should be backed by actual AgentRun events.

---

# 21. Streaming

Responses must stream.

Support:

* text tokens
* thinking/reasoning status where supported by the model/API
* tool calls
* tool results
* file changes
* progress events

Define structured events:

message_start
text_delta
tool_call
tool_result
file_change
artifact_update
agent_status
message_complete
error

---

# 22. Model routing

Implement a ModelRouter.

Tasks should be routable:

simple_chat
coding
deep_reasoning
summarization
retrieval
embedding
reranking
vision
background_memory

Example:

simple_chat -> FAST_MODEL
deep_reasoning -> MAIN_MODEL
embedding -> EMBEDDING_MODEL
reranking -> RERANKER_MODEL
vision -> VISION_MODEL

The user can override routing manually.

---

# 23. Local-first requirements

Default behavior:

NO cloud APIs required.

The complete core application must function with:

* local LLM
* local embeddings
* local reranking
* local database
* local vector store
* local filesystem
* local tools

Cloud features should be optional adapters.

---

# 24. Privacy

Default:

* no telemetry
* no external logging
* no analytics
* no cloud storage
* no automatic upload
* no external API requests

Provide a visible "Local only" status.

---

# 25. Configuration

Create .env.example.

Example:

LLM_BASE_URL=http://localhost:1234/v1
MAIN_MODEL=...
FAST_MODEL=...
VISION_MODEL=...
EMBEDDING_MODEL=...
RERANKER_MODEL=...

DATABASE_URL=sqlite:///./data/app.db
VECTOR_DB_PATH=./data/vector

WORKSPACE_PATH=./workspace

TERMINAL_ENABLED=false
WEB_SEARCH_ENABLED=false

All configurable.

---

# 26. Testing requirements

Create automated tests for:

* context manager
* token budgeting
* retrieval
* reranking
* memory
* conversation compression
* tool validation
* file editing
* patch application
* agent loop
* permissions
* sandbox execution
* artifact versioning

Also create integration tests using a mock LLM provider.

The application must not require a real model for unit tests.

---

# 27. Observability

Implement local debugging logs.

Each agent run should have:

run_id
timestamp
model
tokens_in
tokens_out
latency
tools_called
retrieval_results
files_modified
errors

Provide a developer/debug panel.

This is important for diagnosing why the local model performs poorly.

---

# 28. Failure handling

The system should gracefully recover from:

* model timeout
* malformed tool calls
* tool failure
* invalid JSON
* context overflow
* missing file
* failed patch
* failed build
* sandbox timeout
* model unavailable

Never crash the entire conversation because one tool failed.

---

# 29. Important model behavior

Do NOT put massive behavioral instructions into the system prompt.

Use structured state and tools.

The model should receive:

* concise system policy
* current objective
* relevant retrieved context
* available tools
* current tool state
* necessary project instructions

Avoid sending irrelevant skills or project files.

---

# 30. Development phases

Implement in this order.

PHASE 1

* project structure
* frontend
* backend
* local model connection
* streaming chat
* persistent conversations

PHASE 2

* project/workspace system
* file browser
* attachments
* indexing

PHASE 3

* embeddings
* hybrid retrieval
* reranking
* context manager

PHASE 4

* agent runtime
* tool registry
* filesystem tools
* terminal

PHASE 5

* memory
* conversation compression
* skills

PHASE 6

* artifacts
* diff-based editing
* version history

PHASE 7

* sandbox
* Git
* Unreal Engine tools

PHASE 8

* vision
* web/search adapters
* advanced model routing

Do not attempt to implement all phases in one giant file or one monolithic service.

---

# 31. Code quality requirements

Use:

* strict TypeScript
* typed Python
* Pydantic
* clear interfaces
* dependency injection where appropriate
* modular services
* no duplicated business logic
* no hard-coded machine paths
* no secrets in source code

Every major component should have a clear interface.

---

# 32. Most important requirement

The final system should make a 9B local model substantially more useful by giving it:

context management
+
retrieval
+
memory
+
tools
+
workspace
+
agent loops
+
skills.

Do not attempt to solve poor model performance primarily by adding more prompt text.

Architecture should compensate for the model's limitations.

---

# 33. Deliverables

Create:

1. full monorepo
2. frontend
3. backend
4. database schema
5. vector retrieval subsystem
6. model abstraction
7. agent runtime
8. tool registry
9. workspace/project system
10. memory system
11. skill system
12. artifact system
13. terminal sandbox abstraction
14. Unreal Engine skill
15. test suite
16. documentation
17. .env.example
18. setup script
19. local development instructions
20. production build instructions

At the end, provide:

* architecture diagram
* directory tree
* setup instructions
* supported local inference backends
* model configuration instructions
* explanation of every service
* known limitations
* next recommended implementation steps

Do not create fake functionality.

Where a subsystem is not implemented yet, expose a clear interface/stub rather than pretending it works.

Build a working Phase 1 first, then progressively implement subsequent phases without breaking the existing architecture.
