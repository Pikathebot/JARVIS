import asyncio
import contextlib
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional
from fastapi import FastAPI, HTTPException, Query, Request, UploadFile, File, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.config import settings
from app.agent.orchestrator import AgentOrchestrator
from app.agent.model_router import ModelRouter
from app.agent.openrouter_client import OpenRouterClient
from app.agent.provider_factory import get_model_provider
from app.agent.runtime_process_manager import get_runtime_process_manager
from app.governor.resource_governor import (
    ResourceGovernor,
    SystemMetrics,
    ActivityType,
    GovernorStatus,
    GovernorEvent,
)
from app.governor.process_watcher import ProcessWatcher
from app.memory.store import MemoryStore
from app.memory.ephemeral import EphemeralMemoryStore, ephemeral_registry
from app.memory.compactor import ContextCompactor
from app.memory.manager import MemoryManager
from app.skills.loader import SkillsLoader
from app.mcp.manager import MCPManager
from app.voice.wake_word import WakeWordDetector
from app.voice.transcriber import AudioTranscriber
from app.voice.synthesizer import VoiceSynthesizer
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

# Seconds after startup before the TTS model loads: past the client's first fetches and its
# window-assembly animation (see the warm-up in the lifespan below).
KOKORO_WARMUP_DELAY_S = 8.0
# Fire-and-forget startup tasks, held so the event loop's weak references can't drop them.
_background_tasks: set[asyncio.Task] = set()


_REVISION_LINE = re.compile(r"""^(down_revision|revision)\b[^=\n]*=\s*["']([^"']+)["']""", re.MULTILINE)


def _database_at_head(backend_dir: Path) -> bool:
    """
    True only when the SQLite database's stamped revision is the single head of the migration
    chain. Checked without Alembic: importing it (and the SQL dialects it pulls in) cost ~0.9 s
    of every backend start, to learn on almost every start that there was nothing to do.
    Anything unexpected -- not SQLite, no version table, a branch, an unparsable file -- answers
    False, and the full Alembic upgrade runs as before.
    """
    url = settings.database_url_resolved
    if not url.startswith("sqlite:///"):
        return False
    revisions: set[str] = set()
    parents: set[str] = set()
    for script in (backend_dir / "alembic" / "versions").glob("*.py"):
        found = dict(
            (key, value) for key, value in _REVISION_LINE.findall(script.read_text(encoding="utf-8"))
        )
        if "revision" not in found:
            return False
        revisions.add(found["revision"])
        if "down_revision" in found:
            parents.add(found["down_revision"])
    heads = revisions - parents
    if len(heads) != 1:
        return False
    path = Path(url[len("sqlite:///"):])
    if not path.exists():
        return False
    import sqlite3

    try:
        with contextlib.closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as db:
            stamped = [row[0] for row in db.execute("SELECT version_num FROM alembic_version")]
    except sqlite3.Error:
        return False
    return stamped == list(heads)


def run_db_migrations() -> None:
    """Run pending Alembic database migrations synchronously in a worker thread."""
    try:
        backend_dir = Path(__file__).resolve().parent.parent
        if _database_at_head(backend_dir):
            logger.info("Database schema is current; no migrations to run.")
            return
        from alembic import command
        from alembic.config import Config

        alembic_ini_path = backend_dir / "alembic.ini"
        if alembic_ini_path.exists():
            alembic_cfg = Config(str(alembic_ini_path))
            # In-process: keep the app's logging (see alembic/env.py).
            alembic_cfg.attributes["configure_logging_skipped"] = True
            alembic_cfg.set_main_option("script_location", str(backend_dir / "alembic"))
            alembic_cfg.set_main_option("sqlalchemy.url", settings.database_url_resolved)
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



async def reload_paged_model() -> bool:
    """Restart llama-server with the model it was serving, so a model WDDM demoted to shared
    memory loads back into VRAM (the governor calls this once the card has room again). The
    launch ladder still sizes the relaunch to the room there is, and announces a downgrade."""
    pm = get_runtime_process_manager()
    kind = pm.current_model_kind
    if not kind:
        return False
    await pm.stop()
    return await pm.ensure_running(kind)


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
    on_throttle_unload=auto_unload_models,
    model_vram_mb_provider=lambda: get_runtime_process_manager().model_vram_mb,
    external_vram_floor_mb=settings.governor_external_vram_floor_mb,
    external_vram_baseline_provider=lambda: get_runtime_process_manager().external_vram_baseline_mb,
    external_vram_growth_mb=settings.governor_external_vram_growth_mb,
    runtime_busy_provider=lambda: get_runtime_process_manager().is_processing(),
    on_paged_model_reload=reload_paged_model,
    paged_reload_reserve_mb=settings.llama_launch_vram_reserve_mb,
)
# The launch ladder asks the governor how much of the card is free before each rung.
get_runtime_process_manager().vram_headroom_provider = (
    lambda: governor.vram_headroom_mb(reserve_mb=settings.llama_launch_vram_reserve_mb)
)
process_watcher = ProcessWatcher(
    config_path=settings.governor_watchlist_path,
    poll_interval=settings.governor_process_poll_interval,
    launch_debounce_seconds=settings.governor_process_launch_debounce,
    recovery_debounce_seconds=settings.governor_process_recovery_debounce,
)
from app.database.session import SessionLocal
from app.database.models import Project
from sqlmodel import select
from app.memory.store import MemoryStore

from app.agent.model_router import ModelRouter

# Initialize global subsystems
memory_store = MemoryStore(session_factory=SessionLocal)
model_router = ModelRouter()
memory_manager = MemoryManager(memory_store=memory_store, model_router=model_router)

compactor = ContextCompactor(
    memory_store=memory_store,
    max_context_tokens=settings.memory_max_context_tokens,
    max_message_count=settings.memory_max_message_count,
    tool_pruning_char_threshold=settings.memory_tool_pruning_char_threshold
)
skills_loader = SkillsLoader()
mcp_manager = MCPManager()

wake_detector = WakeWordDetector()
transcriber = AudioTranscriber()
synthesizer = VoiceSynthesizer()

# Ambient awareness: notices hardware conditions worth speaking up about.
awareness_monitor = AwarenessMonitor(
    governor=governor,
    poll_seconds=settings.awareness_poll_seconds,
    restate_cooldown_seconds=settings.awareness_restate_cooldown_seconds,
)
awareness_monitor.enabled = settings.awareness_enabled


def _announce_launch_adjustment(adjustment: dict) -> None:
    """
    A slot that could not get its full configuration says so through the awareness channel --
    once per change, like every other thing Jarvis reports unprompted. Silently serving a
    text-only or smaller model is how "Jarvis got worse" turns into a mystery.
    """
    import re

    from app.awareness.observations import Observation, Severity

    def friendly(filename: object) -> str:
        # "Qwen3.5-9B-UD-IQ3_XXS.gguf" -> "Qwen3.5-9B": the quant suffix is noise when spoken.
        stem = str(filename or "").removesuffix(".gguf")
        return re.sub(r"[-_.](?:UD[-_])?(?:I?Q\d[\w]*|F16|BF16|F32)$", "", stem) or stem

    step = adjustment.get("step")
    served = friendly(adjustment.get("served_model"))
    requested = friendly(adjustment.get("requested_model"))
    slot = adjustment.get("slot", "main")
    headroom = adjustment.get("headroom_mb")
    why = f" The card had {headroom:,} megabytes to spare." if isinstance(headroom, (int, float)) else ""
    if step == "restored":
        title, detail, severity = (
            f"{slot} model back at full configuration",
            f"{served} is running with its projector and full context again.",
            Severity.INFO,
        )
        spoken = f"The {slot} model is back to its full configuration."
    elif step == "no_projector":
        title, detail, severity = (
            f"{slot} model running without vision",
            f"{served} launched text-only: its projector did not fit beside the rest of the desktop.{why}",
            Severity.NOTICE,
        )
        spoken = f"I am running the {slot} model without vision for now; the graphics card is nearly full."
    elif step == "reduced_context":
        title, detail, severity = (
            f"{slot} model running with reduced context",
            f"{served} launched text-only with {adjustment.get('ctx')} tokens of context instead of "
            f"{adjustment.get('requested_ctx')}.{why}",
            Severity.NOTICE,
        )
        spoken = f"I am running the {slot} model with a smaller memory window; the graphics card is nearly full."
    else:
        title, detail, severity = (
            f"Running {served} instead of {requested}",
            f"{requested} would not fit on the graphics card next to the rest of the desktop, so the "
            f"{slot} slot is being served by {served}.{why}",
            Severity.WARNING,
        )
        spoken = f"I am using the smaller model for now; {requested} does not fit on the graphics card."
    awareness_monitor.emit(Observation(
        kind="model_budget",
        severity=severity,
        title=title,
        detail=detail,
        spoken=spoken,
        data=dict(adjustment),
        resolved=step == "restored",
    ))


get_runtime_process_manager().on_launch_adjusted = _announce_launch_adjustment


async def _evict_on_vram_critical(observation) -> Optional[str]:
    """
    Proactive action for a CRITICAL vram_pressure observation: evict the
    model ourselves instead of waiting for the governor's own throttle
    threshold (which additionally requires high GPU compute, or >=99% raw
    VRAM) or for llama.cpp to fail an allocation outright.
    """
    if awareness_monitor.last_snapshot and awareness_monitor.last_snapshot.model_unloaded:
        return None
    if governor.is_busy:
        # A turn is in flight: high VRAM is that turn working. Killing llama-server now
        # ends the user's answer with a ReadError (seen live); the governor's own
        # eviction path defers for exactly this reason. Re-evaluated on the next poll.
        logger.info("VRAM critical during an active turn; deferring eviction until idle.")
        return None
    try:
        success = await auto_unload_models()
    except Exception as e:
        logger.warning("Proactive VRAM eviction failed: %s", e)
        return None
    if not success:
        return None
    return "VRAM was critical, so I evicted the model before it ran out."


async def _name_disk_offenders(observation) -> Optional[str]:
    """
    Proactive action for a CRITICAL disk_space observation: say what is actually taking the
    space among the things Jarvis manages (project workspaces and the model files), so the
    warning comes with somewhere to look rather than just a number.
    """
    from app.awareness.actions import describe_disk_offenders

    from app.agent.model_catalog import get_model_catalog

    roots = [Path(settings.workspace_path), get_model_catalog().models_dir]
    try:
        return await asyncio.to_thread(describe_disk_offenders, roots)
    except Exception as e:
        logger.warning("Disk offender scan failed: %s", e)
        return None


awareness_monitor.actions = {
    "vram_pressure": _evict_on_vram_critical,
    "disk_space": _name_disk_offenders,
}
awareness_monitor.process_watchlist = list(getattr(process_watcher, "watchlist", []) or [])
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

    
    # Connect MCP servers in the background: spawning and initialising the built-in server took
    # ~0.26 s, all of it before the backend answered, and the client's window waits on that.
    # Its tools join the registry a moment later -- long before any chat turn can arrive.
    async def _connect_mcp() -> None:
        try:
            await mcp_manager.connect_all()
        except Exception as e:
            logger.warning("Error connecting MCP servers: %s", e)

    mcp_connect = asyncio.create_task(_connect_mcp())
    _background_tasks.add(mcp_connect)
    mcp_connect.add_done_callback(_background_tasks.discard)

    # Start wake word listener
    wake_detector.start_listening()

    if settings.awareness_enabled:
        await awareness_monitor.start()

    if settings.routines_enabled:
        await routine_scheduler.start()

    if settings.confirmation_timeout_seconds > 0:
        await confirmation_watcher.start()

    # Load the local TTS model off the event loop so the first spoken reply is not the slow one
    # (~5 s cold vs ~1 s warm). Nothing waits on it; speech simply loads on demand if it is
    # still running. Delayed: loading takes ~3.5 s at ~50% of the whole CPU (ONNX Runtime uses
    # every core), which landed exactly while the client was assembling its window. Capping
    # its threads was measured and rejected (2026-09-24): the load took as long, and every
    # later reply synthesised 5-50% slower and less predictably (default RTF 0.55; 6-12
    # threads 0.57-0.83 on the 14700HX's mix of P- and E-cores).
    if synthesizer.active_backend == "kokoro":
        async def _warm_tts_later() -> None:
            await asyncio.sleep(KOKORO_WARMUP_DELAY_S)
            await asyncio.get_running_loop().run_in_executor(None, synthesizer.kokoro.warm_up)

        task = asyncio.create_task(_warm_tts_later())
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

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
    wake_detector.stop_listening()
    await awareness_monitor.stop()
    await routine_scheduler.stop()
    await confirmation_watcher.stop()
    try:
        from app.agent.captioner import get_image_captioner
        await get_image_captioner().stop()
    except Exception as e:
        logger.debug("Captioner shutdown: %s", e)
    try:
        from app.rag.embedding_sidecar import get_embedding_sidecar
        get_embedding_sidecar().stop()
    except Exception as e:
        logger.debug("Embedding sidecar shutdown: %s", e)
    if settings.governor_enabled:
        await process_watcher.stop()
        await governor.stop()
    
    # Disconnect MCP servers (after a connect still in flight has been stopped)
    if not mcp_connect.done():
        mcp_connect.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await mcp_connect
    try:
        await mcp_manager.disconnect_all()
    except Exception as e:
        logger.warning("Error disconnecting MCP servers: %s", e)


from fastapi.middleware.cors import CORSMiddleware

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
        description="Chat mode: 'WORKSPACE' (default, project-bound), 'FREEFORM' (no project, "
                    "no workspace, no RAG) or 'SYSTEM'"
    )
    project_id: Optional[str] = Field(
        default=None,
        description="Optional active project ID"
    )
    attachments: Optional[list[dict[str, Any]]] = Field(
        default=None,
        description="Optional list of attached files"
    )
    ephemeral: bool = Field(
        default=False,
        description="Scratchpad turn: the conversation is kept in memory only and never written "
                    "to the database or the session list (see app.memory.ephemeral)"
    )




class ChatResponse(BaseModel):
    response: str
    model: str
    provider: str = Field(default="llama_cpp", description="'llama_cpp' or 'openrouter'")
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
    # The client grants this process the right to raise windows (AllowSetForegroundWindow);
    # without it Windows' foreground lock refuses focus_app.
    pid: int = 0


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
    # What the main slot will actually load: the catalogue's choice (data/models.json) ahead
    # of .env, the same precedence RuntimeProcessManager.resolve_model_path applies.
    from app.agent.model_catalog import get_model_catalog

    configured_model = get_model_catalog().selected("main") or settings.llama_main_model_path

    throttled, _ = await governor.is_throttled()
    openrouter_client = get_openrouter_client()
    sessions_count = await asyncio.to_thread(memory_store.count_sessions)
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
        active_sessions_count=sessions_count,
        active_mcp_servers_count=len([s for s in mcp_servers if s["connected"]]),
        available_skills_count=len(skills),
        voice_enabled=wake_detector.is_listening,
        pid=os.getpid(),
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
            "model_vram_mb": metrics.model_vram_mb,
            "external_vram_mb": metrics.external_vram_mb,
            "external_vram_baseline_mb": metrics.external_vram_baseline_mb,
            "model_resident": metrics.model_resident,
            "gpu_temp_c": metrics.gpu_temp_c,
            "timestamp": metrics.timestamp,
        },
        thresholds={
            "gpu_threshold": governor.gpu_threshold,
            "vram_threshold": governor.vram_threshold,
            "external_vram_floor_mb": governor.external_vram_floor_mb,
            "external_vram_growth_mb": governor.external_vram_growth_mb,
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
async def list_sessions(
    project_id: Optional[str] = None,
    chat_mode: Optional[str] = None,
    limit: int = Query(50, ge=0, le=1000),
):
    """List stored conversation sessions, most recent first. ``chat_mode=FREEFORM`` lists the
    Freeform space's sessions; anything else lists workspace sessions (Freeform ones excluded),
    optionally filtered by project_id. ``limit`` defaults to 50; pass 0 for everything.
    Ephemeral conversations are never stored, so never listed."""
    return await asyncio.to_thread(
        memory_store.list_sessions, project_id=project_id, limit=limit or None, chat_mode=chat_mode
    )



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
    """Delete a conversation session and all its stored messages. For an ephemeral session this
    is the purge: its in-memory context and any files uploaded into it are dropped at once."""
    if ephemeral_registry.purge(session_id):
        return {"deleted": True, "session_id": session_id, "ephemeral": True}
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


# --- Voice Endpoints ---

@app.get("/voice/status")
async def voice_status():
    """Returns voice and wake-word detector status."""
    return {
        "wake_word_active": wake_detector.is_listening,
        "wake_words": wake_detector.wake_words,
        "synthesizer_voice": synthesizer.voice_name,
        "synthesizer_backend": synthesizer.active_backend,
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
    from app.voice.kokoro_engine import KOKORO_VOICES
    return {
        "current": synthesizer.voice_name,
        "backend": synthesizer.active_backend,
        "available": AVAILABLE_NEURAL_VOICES if synthesizer.active_backend == "edge" else KOKORO_VOICES,
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




def resolve_project_id(requested: Optional[str]) -> Optional[str]:
    """The project a chat turn belongs to: what the client asked for, else the server-side
    active project. Without the fallback a client that has not learned the active workspace yet
    files its sessions under no project at all, where no workspace-scoped list ever shows them."""
    if requested:
        return requested
    try:
        with SessionLocal() as db:
            project = db.exec(select(Project).where(Project.is_active == True).limit(1)).first()  # noqa: E712
            return project.id if project else None
    except Exception:
        logger.debug("active project lookup failed; leaving the turn unscoped", exc_info=True)
        return None


def resolve_turn_project_id(request: "ChatRequest") -> Optional[str]:
    """A Freeform turn belongs to no project -- not even the active one."""
    if (request.chat_mode or "").upper() == "FREEFORM":
        return None
    return resolve_project_id(request.project_id)


def turn_memory_store(request: "ChatRequest"):
    """The store a turn's conversation goes to: SQLite, or process memory for an ephemeral one."""
    return EphemeralMemoryStore(memory_store) if request.ephemeral else memory_store


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
    project_id = await asyncio.to_thread(resolve_turn_project_id, request)

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
    orchestrator = AgentOrchestrator(
        openrouter_client=get_openrouter_client(),
        memory_store=turn_memory_store(request),
        compactor=compactor,
        skills_loader=skills_loader,
        mcp_manager=mcp_manager,
    )

    try:
        async with governor.activity(ActivityType.INFERENCING, label=request.session_id or "chat_turn"):
            result = await orchestrator.run(
                user_message=active_message,
                session_id=request.session_id,
                project_id=project_id,
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
    project_id = await asyncio.to_thread(resolve_turn_project_id, request)

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

    orchestrator = AgentOrchestrator(
        openrouter_client=get_openrouter_client(),
        memory_store=turn_memory_store(request),
        compactor=compactor,
        skills_loader=skills_loader,
        mcp_manager=mcp_manager,
    )


    async def event_generator():
        try:
            async with governor.activity(ActivityType.INFERENCING, label=request.session_id or "chat_turn"):
                async for event in orchestrator.run_stream(
                    user_message=active_message,
                    session_id=request.session_id,
                    project_id=project_id,
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
