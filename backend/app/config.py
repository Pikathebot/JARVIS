import os
from pathlib import Path
from typing import Optional, List
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=os.path.join(BASE_DIR, ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True
    )

    # Engine
    llama_server_exe: str = Field(default="tools/llama-cpp/llama-server.exe", alias="LLAMA_SERVER_EXE")
    llama_base_url: str = Field(default="http://localhost:8001/v1", alias="LLAMA_BASE_URL")
    llama_main_model_path: str = Field(default="models/Qwen3.5-9B-Q4_K_M.gguf", alias="LLAMA_MAIN_MODEL_PATH")
    llama_fast_model_path: str = Field(default="models/Qwen3.5-4B-Q4_K_M.gguf", alias="LLAMA_FAST_MODEL_PATH")
    llama_host: str = Field(default="127.0.0.1", alias="LLAMA_HOST")
    llama_port: int = Field(default=8001, alias="LLAMA_PORT")
    llama_startup_timeout_seconds: float = Field(default=90.0, alias="LLAMA_STARTUP_TIMEOUT")
    llama_extra_args: list[str] = Field(default_factory=list, alias="LLAMA_EXTRA_ARGS")
    # Passed through to the llama-server child's environment, not onto its command line: the
    # chat-template kwargs (e.g. Qwen3.5's {"enable_thinking":true}) are only read from the
    # environment by llama-server, so a .env entry alone would never reach it -- pydantic-settings
    # reads .env into Settings, it does not export anything into os.environ.
    llama_chat_template_kwargs: Optional[str] = Field(default=None, alias="LLAMA_CHAT_TEMPLATE_KWARGS")
    # Per-slot override: the fast (4B) slot rambles more in its thinking block than the main (9B)
    # one does at the same setting, so it defaults to thinking off while main keeps it on. Falls
    # back to llama_chat_template_kwargs if unset.
    llama_chat_template_kwargs_fast: Optional[str] = Field(
        default='{"enable_thinking":false}', alias="LLAMA_CHAT_TEMPLATE_KWARGS_FAST"
    )

    # Debug aid: when set, every chat-completions payload sent to llama-server is written to this
    # directory as JSON (one file per request, image data elided) so a "the model ignored its
    # tools" report can be replayed against the server with curl. Off by default.
    llama_payload_dump_dir: Optional[str] = Field(default=None, alias="LLAMA_PAYLOAD_DUMP_DIR")

    # Execution Flags (Amendment 1)
    llama_n_gpu_layers: int = Field(default=99, alias="LLAMA_N_GPU_LAYERS")
    llama_use_mmap: bool = Field(default=False, alias="LLAMA_USE_MMAP")
    # Vision: when the selected model has an mmproj-*.gguf beside it (see model_catalog) it is
    # passed as --mmproj so image attachments are actually seen. Off = text-only even if a
    # projector exists (saves its VRAM); offload=False keeps the projector on the CPU.
    llama_mmproj_enabled: bool = Field(default=True, alias="LLAMA_MMPROJ_ENABLED")
    llama_mmproj_offload: bool = Field(default=True, alias="LLAMA_MMPROJ_OFFLOAD")
    # Image captioner sidecar (app/agent/captioner.py): a small CPU-only vision model that
    # describes image attachments for any model that cannot see them itself. Always cold;
    # started on the first image, stopped after captioner_idle_seconds without one.
    captioner_enabled: bool = Field(default=True, alias="CAPTIONER_ENABLED")
    captioner_model_path: str = Field(
        default="models/captioner/SmolVLM-500M-Instruct-Q8_0.gguf", alias="CAPTIONER_MODEL_PATH"
    )
    captioner_port: int = Field(default=8002, alias="CAPTIONER_PORT")
    captioner_idle_seconds: float = Field(default=300.0, alias="CAPTIONER_IDLE_SECONDS")
    captioner_threads: int = Field(default=0, alias="CAPTIONER_THREADS")  # 0 = llama-server default

    # Context Limits (Amendment 2)
    llama_ctx_size_main: int = Field(default=16384, alias="LLAMA_CTX_SIZE_MAIN")
    llama_ctx_size_fast: int = Field(default=8192, alias="LLAMA_CTX_SIZE_FAST")

    # Model Routing (Section 2 of Build plan)
    main_model: str = Field(default="Qwen3.5-9B-Q4_K_M", alias="MAIN_MODEL")
    fast_model: str = Field(default="Qwen3.5-4B-Q4_K_M", alias="FAST_MODEL")
    vision_model: Optional[str] = Field(default=None, alias="VISION_MODEL")
    embedding_model: str = Field(default="Qwen3-Embedding-0.6B", alias="EMBEDDING_MODEL")
    reranker_model: str = Field(default="Qwen3-Reranker-0.6B", alias="RERANKER_MODEL")

    # Server & App Configuration
    app_host: str = Field(default="127.0.0.1", alias="APP_HOST")
    app_port: int = Field(default=8000, alias="APP_PORT")
    # A relative sqlite path is anchored to the backend directory (see ``database_url_resolved``),
    # not to the process cwd: the app runs with cwd=backend, pytest with cwd=repo root, and the
    # two would otherwise quietly use two different database files.
    database_url: str = Field(default="sqlite:///./data/jarvis_memory.db", alias="DATABASE_URL")
    workspace_path: str = Field(default="./workspace", alias="WORKSPACE_PATH")
    allowed_cors_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:8000",
            "http://127.0.0.1:8000",
            "vscode-webview://*"
        ],
        alias="ALLOWED_CORS_ORIGINS"
    )
    max_upload_size_mb: int = Field(default=50, alias="MAX_UPLOAD_SIZE_MB")

    # Persona (how Jarvis speaks; tool protocol is invariant)
    persona_id: str = Field(default="jarvis", alias="PERSONA_ID")

    # Ambient awareness (proactive hardware observations)
    awareness_enabled: bool = Field(default=True, alias="AWARENESS_ENABLED")
    awareness_poll_seconds: float = Field(default=20.0, alias="AWARENESS_POLL_SECONDS")
    awareness_restate_cooldown_seconds: float = Field(
        default=300.0, alias="AWARENESS_RESTATE_COOLDOWN_SECONDS"
    )
    # Proactive tool use: let awareness observations trigger real actions
    # (e.g. evict the model before VRAM is exhausted) rather than only report.
    proactive_actions_enabled: bool = Field(default=True, alias="PROACTIVE_ACTIONS_ENABLED")

    # Scheduled routines (time-triggered briefings/messages)
    routines_enabled: bool = Field(default=True, alias="ROUTINES_ENABLED")
    routines_check_seconds: float = Field(default=20.0, alias="ROUTINES_CHECK_SECONDS")

    terminal_enabled: bool = Field(default=False, alias="TERMINAL_ENABLED")

    web_search_enabled: bool = Field(default=False, alias="WEB_SEARCH_ENABLED")

    # Cloud Routing & Heavy Mode Settings
    cloud_routing_enabled: bool = Field(default=False, alias="CLOUD_ROUTING_ENABLED")
    heavy_mode_enabled: bool = Field(default=False, alias="HEAVY_MODE_ENABLED")
    openrouter_enabled: bool = Field(default=False, alias="OPENROUTER_ENABLED")
    openrouter_api_key: Optional[str] = Field(default=None, alias="OPENROUTER_API_KEY")
    openrouter_heavy_model: str = Field(default="meta-llama/llama-3.3-70b-instruct:free", alias="OPENROUTER_HEAVY_MODEL")
    openrouter_base_url: str = Field(default="https://openrouter.ai/api/v1", alias="OPENROUTER_BASE_URL")
    default_routing_mode: str = Field(default="auto", alias="DEFAULT_ROUTING_MODE")

    # Resource Governor V2 Settings
    governor_enabled: bool = Field(default=True, alias="GOVERNOR_ENABLED")
    governor_poll_interval: float = Field(default=1.0, alias="GOVERNOR_POLL_INTERVAL")
    governor_gpu_threshold: float = Field(default=88.0, alias="GOVERNOR_GPU_THRESHOLD")
    governor_vram_threshold: float = Field(default=92.0, alias="GOVERNOR_VRAM_THRESHOLD")
    governor_cpu_threshold: float = Field(default=95.0, alias="GOVERNOR_CPU_THRESHOLD")
    governor_ram_threshold: float = Field(default=98.5, alias="GOVERNOR_RAM_THRESHOLD")
    governor_sustained_breach_polls: int = Field(default=3, alias="GOVERNOR_SUSTAINED_BREACH_POLLS")
    governor_recovery_polls: int = Field(default=2, alias="GOVERNOR_RECOVERY_POLLS")
    governor_startup_grace_seconds: float = Field(default=20.0, alias="GOVERNOR_STARTUP_GRACE_SECONDS")
    # VRAM in use beyond Jarvis's own model before the governor treats it as an external
    # workload worth yielding to (a game, a renderer). A normal desktop is 1-2 GB.
    governor_external_vram_floor_mb: float = Field(default=2048.0, alias="GOVERNOR_EXTERNAL_VRAM_FLOOR_MB")
    governor_queue_timeout_seconds: float = Field(default=3.0, alias="GOVERNOR_QUEUE_TIMEOUT_SECONDS")
    governor_watchlist_path: str = Field(default=os.path.join(BASE_DIR.parent, "governor_watchlist.json"), alias="GOVERNOR_WATCHLIST_PATH")
    governor_process_poll_interval: float = Field(default=2.0, alias="GOVERNOR_PROCESS_POLL_INTERVAL")
    governor_process_launch_debounce: float = Field(default=4.0, alias="GOVERNOR_PROCESS_LAUNCH_DEBOUNCE")
    governor_process_recovery_debounce: float = Field(default=3.0, alias="GOVERNOR_PROCESS_RECOVERY_DEBOUNCE")

    # Memory & Context Compaction Settings
    memory_db_path: str = Field(default=os.path.join(BASE_DIR, "data", "jarvis_memory.db"), alias="MEMORY_DB_PATH")
    memory_max_context_tokens: int = Field(default=8192, alias="MEMORY_MAX_CONTEXT_TOKENS")
    memory_max_message_count: int = Field(default=20, alias="MEMORY_MAX_MESSAGE_COUNT")
    memory_tool_pruning_char_threshold: int = Field(default=200, alias="MEMORY_TOOL_PRUNING_CHAR_THRESHOLD")

    # Safety & Tool Limits Settings (Stage A)
    max_tool_calls_per_turn: int = Field(default=15, alias="MAX_TOOL_CALLS_PER_TURN")
    # Tool execution pipeline (app/agent/tool_pipeline.py): every tool runs off the event loop
    # under a timeout, and oversized results are trimmed (head + tail) before the model sees them.
    tool_default_timeout_seconds: float = Field(default=60.0, alias="TOOL_DEFAULT_TIMEOUT_SECONDS")
    tool_result_max_chars: int = Field(default=12000, alias="TOOL_RESULT_MAX_CHARS")
    confirmation_timeout_action: str = Field(default="deny", alias="CONFIRMATION_TIMEOUT_ACTION")
    # How long an unanswered CONFIRMATION_REQUIRED ask stays valid. When it lapses Jarvis says
    # so (through the awareness monitor) and a late "yes" is refused and re-asked. 0 = never.
    confirmation_timeout_seconds: float = Field(default=90.0, alias="CONFIRMATION_TIMEOUT_SECONDS")

    # Tool-Call Reliability Settings
    reliability_window_size: int = Field(default=30, alias="RELIABILITY_WINDOW_SIZE")
    reliability_floor: float = Field(default=0.75, alias="RELIABILITY_FLOOR")

    # Spoken replies (/api/voice/say). "kokoro" = Kokoro-82M on the CPU, fully local (default);
    # "edge" = Microsoft's edge-tts cloud voices. With kokoro selected and its files missing,
    # speech is skipped unless the cloud fallback is explicitly allowed.
    voice_tts_backend: str = Field(default="kokoro", alias="VOICE_TTS_BACKEND")
    voice_tts_cloud_fallback: bool = Field(default=False, alias="VOICE_TTS_CLOUD_FALLBACK")
    kokoro_model_path: str = Field(
        default=os.path.join(BASE_DIR.parent, "models", "tts", "kokoro", "kokoro-v1.0.onnx"),
        alias="KOKORO_MODEL_PATH",
    )
    kokoro_voices_path: str = Field(
        default=os.path.join(BASE_DIR.parent, "models", "tts", "kokoro", "voices-v1.0.bin"),
        alias="KOKORO_VOICES_PATH",
    )
    kokoro_speed: float = Field(default=1.0, alias="KOKORO_SPEED")

    # TTS & Voice Output Settings
    voice_output_enabled: bool = Field(default=False, alias="VOICE_OUTPUT_ENABLED")
    tts_engine: str = Field(default="chatterbox", alias="TTS_ENGINE")
    tts_vram_required_mb: float = Field(default=2500.0, alias="TTS_VRAM_REQUIRED_MB")
    tts_kokoro_vram_required_mb: float = Field(default=1200.0, alias="TTS_KOKORO_VRAM_REQUIRED_MB")
    tts_chunk_size_chars: int = Field(default=300, alias="TTS_CHUNK_SIZE_CHARS")
    tts_device: str = Field(default="cuda", alias="TTS_DEVICE")

    # Runtime & Legacy Compatibility
    model_runtime: str = Field(default="llama_cpp", alias="MODEL_RUNTIME")

    # Context Engine & RAG Settings (CPU-Only)
    rag_embedding_model: str = Field(default="Qwen/Qwen3-Embedding-0.6B", alias="RAG_EMBEDDING_MODEL")
    rag_reranker_model: str = Field(default="Qwen/Qwen3-Reranker-0.6B", alias="RAG_RERANKER_MODEL")
    rag_device: str = Field(default="cpu", alias="RAG_DEVICE")
    rag_chunk_size: int = Field(default=1024, alias="RAG_CHUNK_SIZE")
    rag_chunk_overlap: int = Field(default=128, alias="RAG_CHUNK_OVERLAP")
    rag_top_k: int = Field(default=5, alias="RAG_TOP_K")

    # Context Manager Budgeting & KV Cache Safeguards
    context_reserved_output_tokens: int = Field(default=2048, alias="CONTEXT_RESERVED_OUTPUT_TOKENS")
    context_tier2_max_attachment_tokens: int = Field(default=8000, alias="CONTEXT_TIER2_MAX_ATTACHMENT_TOKENS")
    context_tier3_max_chunks: int = Field(default=5, alias="CONTEXT_TIER3_MAX_CHUNKS")
    context_tier4_max_messages: int = Field(default=20, alias="CONTEXT_TIER4_MAX_MESSAGES")
    context_include_summary: bool = Field(default=True, alias="CONTEXT_INCLUDE_SUMMARY")

    # Terminal Sandbox Settings (Phase 4)
    terminal_timeout_seconds: int = Field(default=30, alias="TERMINAL_TIMEOUT_SECONDS")

    # Legacy runtime selector, kept for the reliability monitor's rollback logic. It used to
    # default to "bonsai" (an LM Studio-hosted model) from before llama.cpp became the primary
    # runtime -- and since nothing sets it in .env, that stale default silently overrode
    # model_runtime and routed every chat at a model that has not existed for a long time.
    active_model_backend: str = Field(default="llama_cpp", alias="ACTIVE_MODEL_BACKEND")

    lmstudio_base_url: str = Field(default="http://localhost:1234/v1", alias="LMSTUDIO_BASE_URL")


    lmstudio_model: str = Field(default="prism-ml/bonsai-27b", alias="LMSTUDIO_MODEL")
    ollama_host: str = Field(default="http://localhost:11434", alias="OLLAMA_HOST")
    ollama_base_url: str = Field(default="http://localhost:11434", alias="OLLAMA_BASE_URL")
    ollama_model: str = Field(default="hermes3:8b", alias="OLLAMA_MODEL")
    ollama_main_model: str = Field(default="hermes3:8b", alias="OLLAMA_MAIN_MODEL")


    # Backward compatibility properties
    @property
    def llamacpp_server_exe(self) -> str:
        return self.llama_server_exe

    @property
    def llamacpp_main_model_path(self) -> str:
        return self.llama_main_model_path

    @property
    def llamacpp_fast_model_path(self) -> str:
        return self.llama_fast_model_path

    @property
    def llamacpp_host(self) -> str:
        return self.llama_host

    @property
    def llamacpp_port(self) -> int:
        return self.llama_port

    @property
    def llamacpp_ctx_size(self) -> int:
        return self.llama_ctx_size_main

    @property
    def llamacpp_gpu_layers(self) -> int:
        return self.llama_n_gpu_layers

    @property
    def llamacpp_extra_args(self) -> list[str]:
        return self.llama_extra_args

    @property
    def llamacpp_startup_timeout_seconds(self) -> float:
        return self.llama_startup_timeout_seconds

    @property
    def database_url_resolved(self) -> str:
        """``database_url`` with a relative sqlite path made absolute under the backend directory."""
        url = self.database_url
        for prefix in ("sqlite:///./", "sqlite:///"):
            if url.startswith(prefix):
                rest = url[len(prefix):]
                if not os.path.isabs(rest) and not rest.startswith(("/", ":memory:")):
                    return "sqlite:///" + os.path.join(BASE_DIR, rest).replace("\\", "/")
                break
        return url


settings = Settings()
MAX_TOOL_CALLS_PER_TURN: int = settings.max_tool_calls_per_turn
