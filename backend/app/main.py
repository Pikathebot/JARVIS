import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional
from alembic.config import Config
from alembic import command
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
import ollama

from app.config import settings
from app.agent.orchestrator import AgentOrchestrator
from app.agent.model_router import ModelRouter
from app.agent.openrouter_client import OpenRouterClient
from app.agent.lmstudio_client import LMStudioClient
from app.agent.provider_factory import get_model_provider
from app.agent.runtime_process_manager import get_runtime_process_manager
from app.agent.reliability_monitor import ReliabilityMonitor
from app.governor.resource_governor import (
    ResourceGovernor,
    SystemMetrics,
    ActivityType,
    GovernorStatus,
    GovernorEvent,
)
from app.governor.process_watcher import ProcessWatcher
from app.memory.store import MemoryStore
from app.memory.compactor import ContextCompactor
from app.memory.manager import MemoryManager
from app.skills.loader import SkillsLoader
from app.mcp.manager import MCPManager
from app.voice.wake_word import WakeWordDetector
from app.voice.transcriber import AudioTranscriber
from app.voice.synthesizer import VoiceSynthesizer
from app.agent.tts.chatterbox_engine import ChatterboxEngine
from app.agent.tools.audio_playback import is_playing as is_audio_playing, stop_playback as stop_audio_playback
from app.routers import (
    projects_router,
    artifacts_router,
    memories_router,
    persona_router,
    voice_router,
    awareness_router,
    routines_router,
    models_router,
    confirmations_router,
)
from app.awareness.monitor import AwarenessMonitor
from app.persona import persona_manager
from app.routines.scheduler import RoutineScheduler
from app.agent.confirmations import ConfirmationWatcher, get_confirmation_registry



# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("jarvis")


def run_db_migrations() -> None:
    """Run pending Alembic database migrations synchronously in a worker thread."""
    try:
        backend_dir = Path(__file__).resolve().parent.parent
        alembic_ini_path = backend_dir / "alembic.ini"
        if alembic_ini_path.exists():
            alembic_cfg = Config(str(alembic_ini_path))
            alembic_cfg.set_main_option("script_location", str(backend_dir / "alembic"))
            alembic_cfg.set_main_option("sqlalchemy.url", settings.database_url)
            command.upgrade(alembic_cfg, "head")
            logger.info("Database migrations applied successfully.")
        else:
            logger.warning("alembic.ini not found at %s, skipping automatic migration.", alembic_ini_path)
    except Exception as e:
        logger.error("Error applying database migrations: %s", e)


# Initialize global subsystems
async def auto_unload_models() -> bool:
    """Automatically evicts active models from GPU VRAM by stopping llama-server."""
    success = True
    try:
        pm = get_runtime_process_manager()
        await pm.stop()
        logger.info("Governor auto-unload: llama-server process terminated to release GPU VRAM.")
    except Exception as e:
        logger.warning("Error stopping llama-server on auto-unload: %s", e)
        success = False

    return success



governor = ResourceGovernor(
    enabled=settings.governor_enabled,
    poll_interval=settings.governor_poll_interval,
    gpu_threshold=settings.governor_gpu_threshold,
    vram_threshold=settings.governor_vram_threshold,
    cpu_threshold=settings.governor_cpu_threshold,
    ram_threshold=settings.governor_ram_threshold,
    sustained_breach_polls=settings.governor_sustained_breach_polls,
    recovery_polls=settings.governor_recovery_polls,
    startup_grace_seconds=settings.governor_startup_grace_seconds,
    auto_unload_on_throttle=True,
    on_throttle_unload=auto_unload_models
)
process_watcher = ProcessWatcher(
    config_path=settings.governor_watchlist_path,
    poll_interval=settings.governor_process_poll_interval,
    launch_debounce_seconds=settings.governor_process_launch_debounce,
    recovery_debounce_seconds=settings.governor_process_recovery_debounce,
)
from app.database.session import SessionLocal
from app.memory.store import MemoryStore

from app.agent.model_router import ModelRouter

# Initialize global subsystems
memory_store = MemoryStore(session_factory=SessionLocal)
reliability_monitor = ReliabilityMonitor(memory_store=memory_store)
model_router = ModelRouter()
memory_manager = MemoryManager(memory_store=memory_store, model_router=model_router)

compactor = ContextCompactor(
    memory_store=memory_store,
    max_context_tokens=settings.memory_max_context_tokens,
    tool_pruning_char_threshold=settings.memory_tool_pruning_char_threshold
)
skills_loader = SkillsLoader()
mcp_manager = MCPManager()

from app.tools.registry import ToolRegistry
from app.tools.filesystem import (
    ReadFileTool,
    WriteFileTool,
    EditFileTool,
    CreateDirectoryTool,
    ListDirectoryTool,
)
from app.tools.terminal import TerminalExecuteTool
from app.tools.patch_tools import (
    ApplyPatchTool,
    ReplaceRangeTool,
    InsertTool,
    DeleteRangeTool,
)
from app.tools.git_tools import (
    GitStatusTool,
    GitDiffTool,
    GitLogTool,
    GitCheckoutTool,
    GitCommitTool,
)
from app.tools.unreal_tools import (
    UnrealDetectProjectTool,
    UnrealReadLogsTool,
    UnrealBuildTool,
)
from app.tools.web_tools import (
    WebSearchTool,
    WebExtractTool,
)
from app.tools.vision_tools import VisionAnalyzeImageTool

tool_registry = ToolRegistry()
tool_registry.register(ReadFileTool())
tool_registry.register(WriteFileTool())
tool_registry.register(EditFileTool())
tool_registry.register(CreateDirectoryTool())
tool_registry.register(ListDirectoryTool())
tool_registry.register(TerminalExecuteTool())
tool_registry.register(ApplyPatchTool())
tool_registry.register(ReplaceRangeTool())
tool_registry.register(InsertTool())
tool_registry.register(DeleteRangeTool())
tool_registry.register(GitStatusTool())
tool_registry.register(GitDiffTool())
tool_registry.register(GitLogTool())
tool_registry.register(GitCheckoutTool())
tool_registry.register(GitCommitTool())
tool_registry.register(UnrealDetectProjectTool())
tool_registry.register(UnrealReadLogsTool())
tool_registry.register(UnrealBuildTool())
tool_registry.register(WebSearchTool())
tool_registry.register(WebExtractTool())
tool_registry.register(VisionAnalyzeImageTool())


wake_detector = WakeWordDetector()
transcriber = AudioTranscriber()
synthesizer = VoiceSynthesizer()
chatterbox_engine = ChatterboxEngine(governor=governor)

# Ambient awareness: notices hardware conditions worth speaking up about.
awareness_monitor = AwarenessMonitor(
    governor=governor,
    poll_seconds=settings.awareness_poll_seconds,
    restate_cooldown_seconds=settings.awareness_restate_cooldown_seconds,
)
awareness_monitor.enabled = settings.awareness_enabled


async def _evict_on_vram_critical(observation) -> Optional[str]:
    """
    Proactive action for a CRITICAL vram_pressure observation: evict the
    model ourselves instead of waiting for the governor's own throttle
    threshold (which additionally requires high GPU compute, or >=99% raw
    VRAM) or for llama.cpp to fail an allocation outright.
    """
    if awareness_monitor.last_snapshot and awareness_monitor.last_snapshot.model_unloaded:
        return None
    try:
        success = await auto_unload_models()
    except Exception as e:
        logger.warning("Proactive VRAM eviction failed: %s", e)
        return None
    if not success:
        return None
    return "VRAM was critical, so I evicted the model before it ran out."


awareness_monitor.actions = {"vram_pressure": _evict_on_vram_critical}
awareness_monitor.actions_enabled = settings.proactive_actions_enabled

# Scheduled routines: time-triggered briefings/messages, delivered through
# the same observation channel as ambient awareness.
routine_scheduler = RoutineScheduler(
    monitor=awareness_monitor,
    persona_provider=persona_manager.get_active,
    check_seconds=settings.routines_check_seconds,
)
routine_scheduler.enabled = settings.routines_enabled

# Unanswered confirmations: announced through the same observation channel when they lapse,
# so a hands-free session hears that nothing was done rather than just going quiet.
confirmation_watcher = ConfirmationWatcher(
    registry=get_confirmation_registry(),
    monitor=awareness_monitor,
    persona_provider=persona_manager.get_active,
)



@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager to start/stop Resource Governor, Process Watcher, MCP servers, and Voice engine."""
    logger.info("Starting up Jarvis Assistant backend services...")

    # Run database migrations in background thread (non-blocking for async event loop)
    await asyncio.to_thread(run_db_migrations)

    if settings.governor_enabled:
        await governor.start()
        await process_watcher.start(governor)

    
    # Connect MCP servers
    try:
        await mcp_manager.connect_all()
    except Exception as e:
        logger.warning("Error connecting MCP servers: %s", e)

    # Start wake word listener
    wake_detector.start_listening()

    if settings.awareness_enabled:
        await awareness_monitor.start()

    if settings.routines_enabled:
        await routine_scheduler.start()

    if settings.confirmation_timeout_seconds > 0:
        await confirmation_watcher.start()

    logger.info("==================================================================")
    logger.info("  JARVIS Backend is READY and actively listening for requests!")
    logger.info("  Health endpoint: http://127.0.0.1:8000/health")
    logger.info("  API Docs:        http://127.0.0.1:8000/docs")
    logger.info("==================================================================")
    print("\n" + "="*66, flush=True)
    print("  JARVIS Backend is READY and actively listening for requests!", flush=True)
    print("  Health endpoint: http://127.0.0.1:8000/health", flush=True)
    print("  API Docs:        http://127.0.0.1:8000/docs", flush=True)
    print("="*66 + "\n", flush=True)

    yield
    
    logger.info("Shutting down Jarvis Assistant backend services...")
    stop_audio_playback()
    chatterbox_engine.unload_model()
    wake_detector.stop_listening()
    await awareness_monitor.stop()
    await routine_scheduler.stop()
    await confirmation_watcher.stop()
    if settings.governor_enabled:
        await process_watcher.stop()
        await governor.stop()
    
    # Disconnect MCP servers
    try:
        await mcp_manager.disconnect_all()
    except Exception as e:
        logger.warning("Error disconnecting MCP servers: %s", e)


from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path

app = FastAPI(
    title="Local Jarvis Assistant API",
    description="FastAPI backend for local Jarvis Assistant with Voice Wake-Word, MCP Integration, Dynamic Skills, Memory, Governor, and Permissions",
    version="0.8.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(projects_router)
app.include_router(artifacts_router)
app.include_router(memories_router)
app.include_router(persona_router)
app.include_router(voice_router)
app.include_router(awareness_router)
app.include_router(routines_router)
app.include_router(models_router)
app.include_router(confirmations_router)



def _get_ui_directory() -> Optional[Path]:
    import sys
    import os

    use_legacy = os.environ.get("JARVIS_USE_LEGACY_UI", "").lower() in ("1", "true", "yes")

    root = Path(__file__).resolve().parent.parent.parent
    if getattr(sys, "frozen", False):
        if hasattr(sys, "_MEIPASS"):
            root = Path(sys._MEIPASS)
        else:
            root = Path(sys.executable).resolve().parent

    candidates = []
    if not use_legacy:
        # Prefer canonical desktop-app/out (Next.js / React build)
        candidates.extend([
            root / "desktop-app" / "out",
            Path.cwd() / "desktop-app" / "out",
            Path(__file__).resolve().parent.parent / "desktop-app" / "out",
        ])

    # Fallback to legacy desktop/ui
    candidates.extend([
        root / "desktop" / "ui",
        Path.cwd() / "desktop" / "ui",
        Path(__file__).resolve().parent.parent / "desktop" / "ui",
    ])

    for c in candidates:
        if c.exists() and (c / "index.html").exists():
            return c
    return None

UI_DIR = _get_ui_directory()
if UI_DIR:
    next_dir = UI_DIR / "_next"
    if next_dir.exists() and next_dir.is_dir():
        app.mount("/_next", StaticFiles(directory=str(next_dir)), name="next_assets")
    app.mount("/ui", StaticFiles(directory=str(UI_DIR), html=True), name="ui")




def get_ollama_client() -> Optional[ollama.AsyncClient]:
    try:
        return ollama.AsyncClient(host=settings.ollama_host or "http://localhost:11434")
    except Exception:
        return None


def get_lmstudio_client() -> Optional[LMStudioClient]:
    try:
        return LMStudioClient(
            base_url=settings.lmstudio_base_url or "http://localhost:1234/v1",
            timeout=180.0
        )
    except Exception:
        return None


def get_openrouter_client() -> OpenRouterClient:
    return OpenRouterClient(
        api_key=settings.openrouter_api_key,
        base_url=settings.openrouter_base_url
    )




class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, description="User prompt or message")
    session_id: Optional[str] = Field(default="default", description="Conversation session ID")
    model: Optional[str] = Field(
        default=None,
        description="Override model to use for this request (defaults to configured model)"
    )
    mode: Optional[str] = Field(
        default=None,
        description="Routing mode: 'auto' (default), 'normal' (local Bonsai/Hermes), or 'heavy' (OpenRouter)"
    )
    system_prompt: Optional[str] = Field(
        default=None,
        description="Optional system prompt to guide model behavior"
    )
    approved_action_ids: Optional[list[str]] = Field(
        default=None,
        description="List of action ID tokens explicitly approved by the user"
    )
    chat_mode: Optional[str] = Field(
        default="WORKSPACE",
        description="Chat mode: 'WORKSPACE' (default) or 'SYSTEM'"
    )
    project_id: Optional[str] = Field(
        default=None,
        description="Optional active project ID"
    )
    attachments: Optional[list[dict[str, Any]]] = Field(
        default=None,
        description="Optional list of attached files"
    )




class ChatResponse(BaseModel):
    response: str
    model: str
    provider: str = Field(default="lmstudio", description="'lmstudio', 'ollama', or 'openrouter'")
    status: str = Field(default="completed", description="'completed' or 'confirmation_required'")
    session_id: str = Field(default="default", description="Active session ID")
    route_reason: str = Field(default="", description="Reason for model and provider selection")
    fallback_used: bool = Field(default=False, description="True if fallback to local model occurred")
    compaction_performed: Optional[dict[str, Any]] = Field(default=None, description="Compaction audit details if triggered")
    active_skills: list[str] = Field(default_factory=list, description="List of dynamically matched skill names")
    tools_used: list[dict[str, Any]] = Field(default_factory=list)
    pending_confirmations: list[dict[str, Any]] = Field(default_factory=list)
    spoken: Optional[str] = Field(default=None, description="TTS-ready confirmation prompt, if any")


class SpeakRequest(BaseModel):
    text: str = Field(..., min_length=1, description="Text to synthesize for speech")


class TranscribeRequest(BaseModel):
    text: Optional[str] = None


class HealthResponse(BaseModel):
    status: str
    active_backend: str
    configured_model: str
    llama_base_url: str
    llama_connected: bool
    available_models: list[str]
    governor_throttled: bool
    openrouter_configured: bool
    active_sessions_count: int
    active_mcp_servers_count: int
    available_skills_count: int
    voice_enabled: bool


class GovernorPauseRequest(BaseModel):
    reason: str = "manual override"


class GovernorResumeOverrideRequest(BaseModel):
    duration_seconds: Optional[float] = None


class GovernorStatusResponse(BaseModel):
    enabled: bool
    status: str
    throttled: bool
    raw_throttled: bool = False
    throttle_reasons: list[str] = []
    is_manual_override: bool = False
    manual_override_active: bool = False
    override_expires_at: Optional[float] = None
    pending_reload: bool = False
    active_activities: list[str] = []
    model_unloaded: bool = False
    metrics: dict[str, Any]
    thresholds: dict[str, float]


@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check endpoint verifying backend status, llama.cpp, governor, MCP, skills, and voice."""
    primary_provider = get_model_provider()
    primary_connected = await primary_provider.health_check()
    available_models: list[str] = []

    if primary_connected:
        try:
            available_models = await primary_provider.list_models()
        except Exception:
            available_models = []

    active_backend = getattr(settings, "model_runtime", "llama_cpp")
    configured_model = settings.llama_main_model_path

    throttled, _ = await governor.is_throttled()
    openrouter_client = get_openrouter_client()
    sessions = await asyncio.to_thread(memory_store.list_sessions)
    mcp_servers = mcp_manager.list_servers()
    skills = skills_loader.list_skills()

    return HealthResponse(
        status="ok" if not throttled else "degraded",
        active_backend=active_backend,
        configured_model=configured_model,
        llama_base_url=settings.llama_base_url,
        llama_connected=primary_connected,
        available_models=available_models,
        governor_throttled=throttled,
        openrouter_configured=openrouter_client.is_configured and settings.openrouter_enabled,
        active_sessions_count=len(sessions),
        active_mcp_servers_count=len([s for s in mcp_servers if s["connected"]]),
        available_skills_count=len(skills),
        voice_enabled=wake_detector.is_listening
    )



@app.get("/governor/status", response_model=GovernorStatusResponse)
async def governor_status():
    """Returns real-time telemetry metrics and resource governor status."""
    metrics = await governor.get_metrics()
    return GovernorStatusResponse(
        enabled=governor.enabled,
        status=governor.status.value,
        throttled=metrics.throttled,
        raw_throttled=metrics.raw_throttled,
        throttle_reasons=metrics.throttle_reasons,
        is_manual_override=governor.is_manual_override,
        manual_override_active=governor.manual_override_active,
        override_expires_at=governor.override_expires_at,
        pending_reload=governor.pending_reload,
        active_activities=governor.active_activity_types,
        model_unloaded=governor.model_unloaded,
        metrics={
            "cpu_percent": metrics.cpu_percent,
            "ram_percent": metrics.ram_percent,
            "ram_used_mb": metrics.ram_used_mb,
            "ram_total_mb": metrics.ram_total_mb,
            "gpu_available": metrics.gpu_available,
            "gpu_name": metrics.gpu_name,
            "gpu_util_percent": metrics.gpu_util_percent,
            "vram_util_percent": metrics.vram_util_percent,
            "vram_used_mb": metrics.vram_used_mb,
            "vram_total_mb": metrics.vram_total_mb,
            "vram_free_mb": metrics.vram_free_mb,
            "gpu_temp_c": metrics.gpu_temp_c,
            "timestamp": metrics.timestamp,
        },
        thresholds={
            "gpu_threshold": governor.gpu_threshold,
            "vram_threshold": governor.vram_threshold,
            "cpu_threshold": governor.cpu_threshold,
            "ram_threshold": governor.ram_threshold,
        }
    )


@app.post("/governor/pause")
async def governor_pause(req: Optional[GovernorPauseRequest] = None):
    """Forces governor into PAUSED state. Blocks request execution."""
    reason = req.reason if req and req.reason else "manual override"
    governor.force_pause(reason=reason)
    return {"status": "ok", "governor_status": governor.status.value, "reason": reason}


@app.post("/governor/resume")
async def governor_resume():
    """Clears manual pause and override states, restoring automated governance."""
    governor.force_resume()
    return {"status": "ok", "governor_status": governor.status.value}


@app.post("/governor/resume-override")
async def governor_resume_override(req: Optional[GovernorResumeOverrideRequest] = None):
    """Forces governor to report healthy/NORMAL regardless of load for a duration."""
    duration = req.duration_seconds if req else None
    governor.force_resume_ignore_metrics(duration_seconds=duration)
    return {"status": "ok", "governor_status": governor.status.value, "duration_seconds": duration}


@app.post("/governor/clear-error")
async def governor_clear_error():
    """Clears governor error state, restoring automated governance."""
    governor.clear_error()
    return {"status": "ok", "governor_status": governor.status.value}


@app.post("/governor/force-reload")
async def governor_force_reload():
    """Manually triggers model reload into GPU VRAM."""
    initiated = governor.force_reload()
    return {
        "status": "ok" if initiated else "noop",
        "governor_status": governor.status.value,
        "initiated": initiated
    }


@app.get("/governor/history")
async def governor_history(limit: int = 20):
    """Returns recent status transition events (most recent first)."""
    events = governor.get_history(limit=limit)
    return [
        {
            "timestamp": e.timestamp,
            "from_status": e.from_status,
            "to_status": e.to_status,
            "raw_reasons": e.raw_reasons,
            "active_activities": e.active_activities,
            "metrics_snapshot": {
                "cpu_percent": e.metrics_snapshot.cpu_percent,
                "ram_percent": e.metrics_snapshot.ram_percent,
                "gpu_util_percent": e.metrics_snapshot.gpu_util_percent,
                "vram_util_percent": e.metrics_snapshot.vram_util_percent,
            } if e.metrics_snapshot else None
        }
        for e in events
    ]


@app.get("/sessions")
async def list_sessions(project_id: Optional[str] = None):
    """List all stored conversation sessions, optionally filtered by project_id."""
    return await asyncio.to_thread(memory_store.list_sessions, project_id=project_id)



@app.get("/sessions/{session_id}/messages")
async def get_session_messages(session_id: str):
    """Retrieve full message history for a specific session."""
    return await asyncio.to_thread(memory_store.get_messages, session_id)


@app.get("/sessions/{session_id}/compactions")
async def get_session_compactions(session_id: str):
    """Retrieve compaction audit events for a session."""
    return await asyncio.to_thread(memory_store.get_compaction_events, session_id)


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    """Delete a conversation session and all its stored messages."""
    deleted = await asyncio.to_thread(memory_store.delete_session, session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found.")
    return {"deleted": True, "session_id": session_id}



@app.get("/skills")
async def list_skills():
    """List all discovered markdown skills and their trigger keywords."""
    return skills_loader.list_skills()


@app.post("/skills/reload")
async def reload_skills():
    """Hot-reload markdown skills from disk."""
    skills = skills_loader.load_skills()
    return {"reloaded": True, "count": len(skills), "skills": list(skills.keys())}


@app.get("/mcp/servers")
async def list_mcp_servers():
    """List registered MCP servers and their active tool schemas."""
    return mcp_manager.list_servers()


# --- Tool Reliability & Rollback Endpoints (Stage B Addendum) ---

class SwitchBackendRequest(BaseModel):
    backend: str = Field(..., description="Target model backend ('bonsai' or 'hermes3')")
    reason: Optional[str] = Field(default="manual override", description="Reason for switching backend")


@app.get("/reliability/status")
async def get_reliability_status(model_tier: str = "tier2"):
    """Returns real-time tool-call reliability metrics over the rolling window."""
    return reliability_monitor.get_status(model_tier=model_tier)


@app.get("/reliability/history")
async def get_reliability_history(limit: int = 50):
    """Returns recent tool-call reliability alerts and rollback events."""
    return reliability_monitor.get_history(limit=limit)


@app.post("/reliability/switch-backend")
async def switch_model_backend(req: SwitchBackendRequest):
    """Manually switch active model backend (e.g. re-enable Bonsai after manual review)."""
    try:
        result = reliability_monitor.switch_backend(target_backend=req.backend, reason=req.reason or "manual override")
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))



# --- Voice Endpoints ---

class VoiceOutputStatus(BaseModel):
    enabled: bool
    engine: str
    vram_required_mb: float
    is_loaded: bool
    is_playing: bool


class VoiceOutputToggleRequest(BaseModel):
    enabled: bool


@app.get("/api/voice/output", response_model=VoiceOutputStatus)
@app.get("/voice/output", response_model=VoiceOutputStatus)
async def get_voice_output_status():
    """Returns runtime status of spoken voice output and audio engine."""
    return VoiceOutputStatus(
        enabled=settings.voice_output_enabled,
        engine=settings.tts_engine,
        vram_required_mb=settings.tts_vram_required_mb,
        is_loaded=chatterbox_engine.is_loaded(),
        is_playing=is_audio_playing()
    )


@app.post("/api/voice/output", response_model=VoiceOutputStatus)
@app.post("/voice/output", response_model=VoiceOutputStatus)
async def toggle_voice_output(req: VoiceOutputToggleRequest):
    """Enables or disables runtime voice output and stops playback if disabled."""
    settings.voice_output_enabled = req.enabled
    if not req.enabled:
        stop_audio_playback()
    return VoiceOutputStatus(
        enabled=settings.voice_output_enabled,
        engine=settings.tts_engine,
        vram_required_mb=settings.tts_vram_required_mb,
        is_loaded=chatterbox_engine.is_loaded(),
        is_playing=is_audio_playing()
    )


@app.get("/voice/status")
async def voice_status():
    """Returns voice and wake-word detector status."""
    return {
        "wake_word_active": wake_detector.is_listening,
        "wake_words": wake_detector.wake_words,
        "synthesizer_voice": synthesizer.voice_name,
        "voice_output_enabled": settings.voice_output_enabled,
        "tts_engine_loaded": chatterbox_engine.is_loaded(),
        "is_playing_audio": is_audio_playing()
    }


@app.post("/voice/speak")
async def voice_speak(req: SpeakRequest):
    """Sanitize and prepare text for speech synthesis."""
    result = synthesizer.synthesize(req.text)
    return result


class TTSRequest(BaseModel):
    text: str
    voice: Optional[str] = "en-GB-RyanNeural"


@app.post("/voice/tts")
async def voice_neural_tts(req: TTSRequest):
    """Generates ultra-realistic humanlike neural audio (MP3) for spoken responses."""
    audio_bytes = await synthesizer.generate_neural_audio_bytes(req.text, voice=req.voice)
    if not audio_bytes:
        raise HTTPException(status_code=500, detail="Failed to synthesize neural audio.")
    from fastapi.responses import Response
    return Response(content=audio_bytes, media_type="audio/mpeg")


@app.get("/voice/voices")
async def voice_list_neural():
    """List available studio-grade humanlike voices."""
    from app.voice.synthesizer import AVAILABLE_NEURAL_VOICES
    return {
        "current": synthesizer.voice_name,
        "available": AVAILABLE_NEURAL_VOICES
    }


@app.post("/voice/transcribe")
async def voice_transcribe(file: Optional[UploadFile] = File(None)):
    """Transcribe audio upload bytes into text."""
    if file:
        data = await file.read()
        fmt = file.filename.split(".")[-1] if file.filename else "wav"
        res = transcriber.transcribe_audio_bytes(data, format=fmt)
        return res
    return {"success": False, "text": "", "error": "No audio file provided"}


class UnloadModelRequest(BaseModel):
    model_name: Optional[str] = None


@app.post("/models/unload")
async def unload_models(req: Optional[UnloadModelRequest] = None):
    """Immediately evicts loaded models from GPU VRAM by terminating llama-server process."""
    unloaded_models = []
    try:
        pm = get_runtime_process_manager()
        await pm.stop()
        unloaded_models.append(f"llama_cpp:{settings.llama_main_model_path}")
    except Exception as e:
        logger.warning("Error unloading llama-server: %s", e)

    logger.info("Unloaded models from VRAM: %s", unloaded_models)
    return {
        "success": True,
        "unloaded_models": unloaded_models,
        "message": "Model(s) successfully signaled for GPU VRAM eviction."
    }


class LoadModelRequest(BaseModel):
    model_name: Optional[str] = None
    backend: Optional[str] = None


@app.post("/models/load")
async def load_model_endpoint(req: Optional[LoadModelRequest] = None):
    """Loads/warms up a model into GPU VRAM wrapped with governor.activity(ActivityType.MODEL_LOADING)."""
    target_backend = (req.backend if req and req.backend else getattr(settings, "model_runtime", "llama_cpp")).lower().strip()
    target_model = (
        req.model_name
        if req and req.model_name
        else "main"
    )

    async with governor.activity(ActivityType.MODEL_LOADING, label=target_model):
        pm = get_runtime_process_manager()
        success = await pm.ensure_running(target_model)

    return {
        "success": success,
        "model": target_model,
        "backend": target_backend,
        "message": f"Model '{target_model}' loaded successfully." if success else f"Failed to load '{target_model}'."
    }



@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """
    Main chat endpoint with Voice Wake-Word, MCP Integration, Dynamic Skills Loading,
    SQLite Memory, Model Routing (llama.cpp primary default vs Ollama fallback),
    Resource Governor gating, and Safety Permissions.
    """
    # Check for wake word in message
    detected, wake_word, cleaned_query = wake_detector.detect_in_text(request.message)
    active_message = cleaned_query if detected and cleaned_query else request.message

    # 1. Resource Governor Check (with adaptive queueing wait)
    if settings.governor_enabled:
        is_healthy, reason = await governor.wait_until_healthy(
            timeout_seconds=settings.governor_queue_timeout_seconds
        )
        if not is_healthy:
            logger.warning("Rejecting chat request due to high system load: %s", reason)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Resource Governor active: Request paused/rejected due to heavy system load ({reason}). Please retry once resource load subsides."
            )

    # 2. Agent Orchestration with ModelProvider, Memory, Skills, and MCP Tools
    ollama_client = get_ollama_client()
    lmstudio_client = get_lmstudio_client()
    openrouter_client = get_openrouter_client()
    orchestrator = AgentOrchestrator(
        ollama_client=ollama_client,
        lmstudio_client=lmstudio_client,
        openrouter_client=openrouter_client,
        memory_store=memory_store,
        compactor=compactor,
        skills_loader=skills_loader,
        mcp_manager=mcp_manager,
        reliability_monitor=reliability_monitor,
        tts_engine=chatterbox_engine,
        tool_registry=tool_registry
    )

    try:
        async with governor.activity(ActivityType.INFERENCING, label=request.session_id or "chat_turn"):
            result = await orchestrator.run(
                user_message=active_message,
                session_id=request.session_id,
                project_id=request.project_id,
                requested_mode=request.mode,
                requested_model=request.model,
                system_prompt=request.system_prompt,
                approved_action_ids=request.approved_action_ids,
                chat_mode=request.chat_mode,
                attachments=request.attachments
            )

        return ChatResponse(
            response=result.response,
            model=result.model,
            provider=result.provider,
            status=result.status,
            session_id=result.session_id,
            route_reason=result.route_reason,
            fallback_used=result.fallback_used,
            compaction_performed=result.compaction_performed,
            active_skills=result.active_skills,
            tools_used=result.tools_used,
            pending_confirmations=result.pending_confirmations,
            spoken=result.spoken,
        )

    except Exception as e:
        logger.error("Unexpected error in chat endpoint: %s", str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Chat execution error: {str(e)}"
        )


@app.post("/chat/stream")
async def chat_stream(request: ChatRequest):
    """
    High-speed Server-Sent Events (SSE) streaming chat endpoint.
    Emits real-time token events, tool execution updates, and final metadata.
    """
    detected, wake_word, cleaned_query = wake_detector.detect_in_text(request.message)
    active_message = cleaned_query if detected and cleaned_query else request.message

    if settings.governor_enabled:
        is_healthy, reason = await governor.wait_until_healthy(
            timeout_seconds=settings.governor_queue_timeout_seconds
        )
        if not is_healthy:
            logger.warning("Rejecting chat stream request due to high system load: %s", reason)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Resource Governor active: Request paused/rejected due to heavy system load ({reason}). Please retry once resource load subsides."
            )

    ollama_client = get_ollama_client()
    lmstudio_client = get_lmstudio_client()
    openrouter_client = get_openrouter_client()
    orchestrator = AgentOrchestrator(
        ollama_client=ollama_client,
        lmstudio_client=lmstudio_client,
        openrouter_client=openrouter_client,
        memory_store=memory_store,
        compactor=compactor,
        skills_loader=skills_loader,
        mcp_manager=mcp_manager,
        reliability_monitor=reliability_monitor,
        tts_engine=chatterbox_engine,
        tool_registry=tool_registry
    )


    async def event_generator():
        try:
            async with governor.activity(ActivityType.INFERENCING, label=request.session_id or "chat_turn"):
                async for event in orchestrator.run_stream(
                    user_message=active_message,
                    session_id=request.session_id,
                    project_id=request.project_id,
                    requested_mode=request.mode,
                    requested_model=request.model,
                    system_prompt=request.system_prompt,
                    approved_action_ids=request.approved_action_ids,
                    chat_mode=request.chat_mode,
                    attachments=request.attachments
                ):

                    event_type = event.get("event", "message")
                    data_json = json.dumps(event.get("data", {}))
                    yield f"event: {event_type}\ndata: {data_json}\n\n"
        except Exception as e:
            logger.error("Error in streaming response generator: %s", e)
            err_data = json.dumps({"error": str(e)})
            yield f"event: error\ndata: {err_data}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=True
    )
