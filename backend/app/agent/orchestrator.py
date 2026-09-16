import json
import logging
from pathlib import Path
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional
from sqlmodel import select

from app.config import settings, MAX_TOOL_CALLS_PER_TURN
from app.persona import persona_manager
from app.agent.tools.registry import AVAILABLE_TOOLS, execute_tool, get_tool_schema, get_relevant_tools
from app.agent.permissions import (
    evaluate_tool_calls_batch,
    evaluate_tool_permission,
    check_rate_limit,
    RATE_LIMITS,
    PermissionDecision,
    RiskTier,
    ChatMode,
    build_confirmation_prompt,
)
from app.agent.validator import validate_tool_call, CallHistory, ValidationResult
from app.agent.model_router import ModelRouter, RoutingDecision
from app.agent.model_provider import ModelProvider
from app.agent.provider_factory import get_model_provider
from app.agent.openrouter_client import OpenRouterClient
from app.agent.reliability_monitor import ReliabilityMonitor
from app.memory.store import MemoryStore
from app.memory.compactor import ContextCompactor
from app.skills.loader import SkillsLoader, Skill
from app.mcp.manager import MCPManager
from app.agent.tts.chatterbox_engine import ChatterboxEngine
from app.agent.tools.audio_playback import play_audio, stop_playback

logger = logging.getLogger("jarvis.agent.orchestrator")


@dataclass
class OrchestratorResult:
    response: str
    model: str
    provider: str = "llama_cpp"  # "llama_cpp" | "ollama" | "openrouter"
    status: str = "completed"  # "completed" | "confirmation_required"
    session_id: str = "default"
    route_reason: str = ""
    fallback_used: bool = False
    compaction_performed: Optional[dict[str, Any]] = None
    active_skills: list[str] = field(default_factory=list)
    tools_used: list[dict[str, Any]] = field(default_factory=list)
    pending_confirmations: list[dict[str, Any]] = field(default_factory=list)
    spoken: Optional[str] = None


def extract_tool_calls_from_text(content: str, user_prompt: str = "") -> tuple[str, list[dict[str, Any]]]:
    """
    Extract embedded tool calls outputted as raw text by models:
    1. <tool_call> ... </tool_call> tags
    2. Conversational tool announcements: 'use the write_file function: { ... }'
    3. Markdown ```json { ... } ``` or raw JSON blocks matching known tool argument signatures.
    Returns (cleaned_content, extracted_tool_calls).
    """
    if not content or not isinstance(content, str):
        return content, []

    extracted = []

    # 1. Standard <tool_call>...</tool_call> tags
    tag_pattern = re.compile(r"(?:<tool_call>)?\s*(\{[\s\S]*?\})\s*</tool_call>", re.DOTALL)
    for m in tag_pattern.finditer(content):
        try:
            raw_j = m.group(1).strip().replace("True", "true").replace("False", "false")
            parsed = json.loads(raw_j)
            if isinstance(parsed, dict) and ("name" in parsed or "function" in parsed):
                fn_name = parsed.get("name") or parsed.get("function", {}).get("name")
                fn_args = parsed.get("arguments") or parsed.get("args") or parsed.get("parameters") or parsed.get("params") or parsed.get("function", {}).get("arguments", {})
                if isinstance(fn_args, str):
                    try:
                        fn_args = json.loads(fn_args)
                    except Exception:
                        fn_args = {"query": fn_args}
                if fn_name:
                    extracted.append({
                        "id": f"call_{uuid.uuid4().hex[:8]}",
                        "type": "function",
                        "function": {
                            "name": fn_name,
                            "arguments": fn_args if isinstance(fn_args, dict) else {}
                        }
                    })
        except Exception:
            pass

    if extracted:
        cleaned_content = tag_pattern.sub("", content).strip()
        return cleaned_content, extracted

    # 2. Match conversational tool announcements
    tool_names = {
        "write_file", "patch_file", "find_files", "grep_in_files",
        "read_file", "list_directory", "execute_command", "delete_file",
        "web_search", "fetch_url",
        "launch_app", "focus_app", "set_volume", "mute_toggle", "media_key",
        "get_clipboard", "set_clipboard", "list_processes", "kill_process", "send_toast"
    }
    tool_announcement_regex = re.compile(
        r"(?:use(?: the)?|execute(?: the)?|call(?: the)?|invok(?:e|ing)(?: the)?)\s+`?([a-z_]+)`?(?:\s+tool|\s+function)?[\s\S]*?(```(?:json)?\s*)?(\{[\s\S]*?\})(\s*```)?",
        re.IGNORECASE
    )
    for m in tool_announcement_regex.finditer(content):
        t_name = m.group(1).lower().strip()
        if t_name in tool_names:
            raw_json = m.group(3).strip().replace("True", "true").replace("False", "false")
            try:
                parsed_args = json.loads(raw_json)
                if isinstance(parsed_args, dict):
                    extracted.append({
                        "id": f"call_{uuid.uuid4().hex[:8]}",
                        "type": "function",
                        "function": {
                            "name": t_name,
                            "arguments": parsed_args
                        }
                    })
            except Exception:
                pass

    if extracted:
        cleaned_content = tool_announcement_regex.sub("", content).strip()
        return cleaned_content, extracted

    # 3. Match JSON blocks with unambiguous parameter signatures or name/parameters
    json_block_regex = re.compile(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```|(\{[\s\S]*?\})", re.DOTALL)
    for m in json_block_regex.finditer(content):
        raw_json_str = (m.group(1) or m.group(2) or "").strip().replace("True", "true").replace("False", "false")
        if not raw_json_str.startswith("{") or not raw_json_str.endswith("}"):
            continue
        try:
            parsed = json.loads(raw_json_str)
            if isinstance(parsed, dict):
                cid = f"call_{uuid.uuid4().hex[:8]}"
                if "name" in parsed and ("arguments" in parsed or "args" in parsed or "parameters" in parsed or "params" in parsed):
                    fn_name = parsed["name"]
                    fn_args = parsed.get("arguments") or parsed.get("args") or parsed.get("parameters") or parsed.get("params") or {}
                    extracted.append({"id": cid, "type": "function", "function": {"name": fn_name, "arguments": fn_args}})
                elif "file_path" in parsed and "content" in parsed:
                    extracted.append({"id": cid, "type": "function", "function": {"name": "write_file", "arguments": parsed}})
                elif "file_path" in parsed and "search_block" in parsed:
                    extracted.append({"id": cid, "type": "function", "function": {"name": "patch_file", "arguments": parsed}})
                elif "pattern" in parsed and ("max_matches" in parsed or "case_sensitive" in parsed or "path" in parsed):
                    extracted.append({"id": cid, "type": "function", "function": {"name": "grep_in_files", "arguments": parsed}})
                elif "pattern" in parsed and ("root_dir" in parsed or "*" in str(parsed.get("pattern"))):
                    extracted.append({"id": cid, "type": "function", "function": {"name": "find_files", "arguments": parsed}})
                elif "query" in parsed and ("max_results" in parsed or len(parsed) == 1):
                    extracted.append({"id": cid, "type": "function", "function": {"name": "web_search", "arguments": parsed}})
                elif "url" in parsed and ("max_chars" in parsed or len(parsed) == 1):
                    extracted.append({"id": cid, "type": "function", "function": {"name": "fetch_url", "arguments": parsed}})
                elif "command" in parsed:
                    extracted.append({"id": cid, "type": "function", "function": {"name": "execute_command", "arguments": parsed}})
                elif "name_or_path" in parsed:
                    extracted.append({"id": cid, "type": "function", "function": {"name": "launch_app", "arguments": parsed}})
                elif "name_or_title_substring" in parsed:
                    extracted.append({"id": cid, "type": "function", "function": {"name": "focus_app", "arguments": parsed}})
                elif "level" in parsed and len(parsed) == 1:
                    extracted.append({"id": cid, "type": "function", "function": {"name": "set_volume", "arguments": parsed}})
                elif "pid_or_name" in parsed:
                    extracted.append({"id": cid, "type": "function", "function": {"name": "kill_process", "arguments": parsed}})
                elif "title" in parsed and "message" in parsed:
                    extracted.append({"id": cid, "type": "function", "function": {"name": "send_toast", "arguments": parsed}})
        except Exception:
            pass

    if extracted:
        cleaned_content = json_block_regex.sub("", content).strip()
        return cleaned_content, extracted

    # 4. Infer write_file if a file path is requested and the model generated a code block in markdown
    if not extracted and ("```" in content):
        combined_text = (user_prompt or "") + "\n" + content
        path_matches = re.findall(r"(?:in|to|at|file|into|create)\s+[`'\"]?([a-zA-Z0-9_\-./\\]+\.[a-zA-Z0-9]+)[`'\"]?", combined_text, re.IGNORECASE)
        code_block_match = re.search(r"```(?:[a-zA-Z0-9_\-]+)?\n([\s\S]*?)\n```", content)
        if path_matches and code_block_match:
            target_file_path = path_matches[0].strip().replace("\\", "/")
            if any(target_file_path.endswith(ext) for ext in [".py", ".js", ".ts", ".html", ".css", ".json", ".md", ".txt", ".sh", ".bat", ".ps1", ".yml", ".yaml"]):
                code_content = code_block_match.group(1)
                if len(code_content.strip()) > 10:
                    logger.info("Inferred write_file tool call for '%s' from generated code block", target_file_path)
                    extracted.append({
                        "id": f"call_{uuid.uuid4().hex[:8]}",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": {
                                "file_path": target_file_path,
                                "content": code_content,
                                "overwrite": True
                            }
                        }
                    })
                    return content, extracted

    return content, []


TOOL_PROTOCOL_RULES = (
    "CAPABILITIES: you run locally on Windows with direct access to tools, memory, skills and hardware telemetry.\n"
    "CRITICAL RULES:\n"
    "1. NEVER output conversational plans or raw JSON code blocks in your text describing tools you want to run. When an action is needed, directly invoke the tool.\n"
    "2. If the user explicitly asks to create or save a file on disk (e.g. 'save to test.py' or 'create file ...'), invoke 'write_file(file_path=..., content=...)'. If the user simply asks a coding question, asks to explain something, or asks to write a snippet/script without specifying saving to a file, provide the complete, fully-implemented code directly in markdown in your response.\n"
    "3. When editing or updating code in an existing file, invoke 'patch_file(file_path=..., search_block=..., replacement_block=...)'. If needed, invoke 'read_file' first to see the exact text before patching.\n"
    "4. When searching for words, functions, classes, definitions, or symbols across the codebase/project, ALWAYS invoke 'grep_in_files(pattern=..., path=...)'. Never say a symbol is missing without running grep_in_files first.\n"
    "5. When looking for files or directories by name/pattern/extension, ALWAYS invoke 'find_files(pattern=..., root_dir=...)'.\n"
    "6. When the user asks to search online for real-time web info, live news, or documentation, invoke 'web_search(query=...)'.\n"
    "7. When the user provides a web URL (http/https), invoke 'fetch_url(url=...)'. Never use read_file for web URLs.\n"
    "8. When inspecting or reading a local disk file, invoke 'read_file(file_path=...)'.\n"
    "9. When browsing a directory tree, invoke 'list_directory(path=...)'.\n"
    "10. When running shell commands, terminal tools, or scripts, invoke 'execute_command(command=...)'.\n"
    "11. When opening or launching desktop applications, invoke 'launch_app(name_or_path=...)'. ALWAYS prefer checking or calling 'focus_app(name_or_title_substring=...)' first if a window for that app may already be open, avoiding duplicate application instances.\n"
    "12. When controlling audio volume, invoke 'set_volume(level=...)' (0-100) or 'mute_toggle()'. For media playback, invoke 'media_key(action=...)' ('play_pause', 'next', 'previous', 'stop').\n"
    "13. When reading or writing system clipboard text, invoke 'get_clipboard()' or 'set_clipboard(text=...)'.\n"
    "14. When listing running processes, invoke 'list_processes(filter_name=...)'. When terminating an application or process, invoke 'kill_process(pid_or_name=...)'.\n"
    "15. When sending desktop toast notification alerts, invoke 'send_toast(title=..., message=..., urgent=...)'.\n"
    "16. Strip surrounding quotation marks from user queries if present.\n"
    "17. Always use clean relative workspace paths (e.g. '.', 'backend/app', 'scripts', 'docs')."
)


def build_system_prompt(persona: Optional[Any] = None) -> str:
    """
    Compose the system prompt: persona voice first, then the invariant tool
    protocol. The persona shapes manner only - it can never change what tools
    exist or how they must be invoked.
    """
    active = persona or persona_manager.get_active()
    return f"{active.build_prompt_preamble()}\n\n{TOOL_PROTOCOL_RULES}"


class _LegacyClientAdapter(ModelProvider):
    def __init__(self, client: Any, name_str: str = "legacy"):
        self.client = client
        self._name = name_str

    @property
    def name(self) -> str:
        return self._name

    async def health_check(self) -> bool:
        return True

    async def model_info(self) -> dict[str, Any]:
        return {"provider": self._name}

    async def list_models(self) -> list[str]:
        return []

    async def chat(self, messages, model=None, tools=None, temperature=None, profile="general", timeout=None) -> dict[str, Any]:
        res = await self.client.chat(model=model, messages=messages, tools=tools)
        if isinstance(res, dict):
            return res
        msg = getattr(res, "message", None)
        if msg is not None:
            content = getattr(msg, "content", "") or ""
            tool_calls = getattr(msg, "tool_calls", None)
            return {
                "message": {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": tool_calls
                },
                "raw": res
            }
        return {"message": {"role": "assistant", "content": str(res), "tool_calls": None}, "raw": res}

    async def stream_chat(self, messages, model=None, tools=None, temperature=None, profile="general", timeout=None) -> AsyncIterator[dict[str, Any]]:
        if hasattr(self.client, "chat_stream"):
            async for ev in self.client.chat_stream(model=model, messages=messages, tools=tools):
                if ev.get("type") == "token":
                    yield {"event": "text_delta", "content": ev.get("delta", "")}
                elif ev.get("type") == "done":
                    yield {"event": "done", "raw": ev}
        else:
            res = await self.chat(messages=messages, model=model, tools=tools, temperature=temperature, profile=profile)
            content = res.get("message", {}).get("content", "")
            tcs = res.get("message", {}).get("tool_calls")
            if tcs:
                for tc in tcs:
                    yield {"event": "tool_call", "tool_call": tc}
            yield {"event": "text_delta", "content": content}
            yield {"event": "done", "raw": res}

    async def unload_model(self, model=None) -> bool:
        return True


class AgentOrchestrator:
    """
    Orchestrates communication with the primary local ModelProvider (llama.cpp)
    or fallback provider (Ollama), enforcing hardcoded safety permissions,
    complexity routing, memory persistence, context compaction, dynamic skills loading,
    and MCP tool execution.
    """

    def __init__(
        self,
        provider: Optional[ModelProvider] = None,
        router: Optional[ModelRouter] = None,
        memory_store: Optional[MemoryStore] = None,
        compactor: Optional[ContextCompactor] = None,
        skills_loader: Optional[SkillsLoader] = None,
        mcp_manager: Optional[MCPManager] = None,
        reliability_monitor: Optional[ReliabilityMonitor] = None,
        tts_engine: Optional[ChatterboxEngine] = None,
        voice_output_enabled: Optional[bool] = None,
        context_manager: Optional[Any] = None,
        retriever: Optional[Any] = None,
        tool_registry: Optional[Any] = None,
        agent_loop: Optional[Any] = None,
        # Backward compatibility parameters
        ollama_client: Optional[Any] = None,
        lmstudio_client: Optional[Any] = None,
        openrouter_client: Optional[Any] = None,
    ):
        if provider is not None:
            self.provider = provider
        elif lmstudio_client is not None and type(lmstudio_client).__name__ not in ("LMStudioClient", "NoneType"):
            self.provider = _LegacyClientAdapter(lmstudio_client, "lmstudio")
        elif ollama_client is not None and type(ollama_client).__name__ not in ("AsyncClient", "NoneType"):
            self.provider = _LegacyClientAdapter(ollama_client, "ollama")
        else:
            self.provider = get_model_provider()

        self.router = router or ModelRouter(default_mode=settings.default_routing_mode)
        self.memory_store = memory_store or MemoryStore(db_path=settings.memory_db_path)
        self.compactor = compactor or ContextCompactor(
            max_context_tokens=settings.memory_max_context_tokens,
            tool_pruning_char_threshold=settings.memory_tool_pruning_char_threshold
        )
        self.skills_loader = skills_loader or SkillsLoader()
        self.mcp_manager = mcp_manager or MCPManager()
        self.reliability_monitor = reliability_monitor or ReliabilityMonitor(memory_store=self.memory_store)
        self.tts_engine = tts_engine or ChatterboxEngine()
        self.voice_output_enabled = (
            voice_output_enabled if voice_output_enabled is not None else settings.voice_output_enabled
        )

        from app.memory.context_manager import ContextManager
        from app.rag.retriever import HybridRetriever
        from app.tools.registry import ToolRegistry
        from app.tools.filesystem import (
            ReadFileTool,
            WriteFileTool,
            EditFileTool,
            CreateDirectoryTool,
            ListDirectoryTool,
        )
        from app.tools.terminal import TerminalExecuteTool
        from app.agent.loop import AgentLoop

        self.context_manager = context_manager or ContextManager(memory_store=self.memory_store)
        self.retriever = retriever or HybridRetriever()

        if tool_registry is not None:
            self.tool_registry = tool_registry
        else:
            self.tool_registry = ToolRegistry()
            self.tool_registry.register(ReadFileTool())
            self.tool_registry.register(WriteFileTool())
            self.tool_registry.register(EditFileTool())
            self.tool_registry.register(CreateDirectoryTool())
            self.tool_registry.register(ListDirectoryTool())
            self.tool_registry.register(TerminalExecuteTool())

        self.agent_loop = agent_loop or AgentLoop(tool_registry=self.tool_registry)


        self.openrouter_client = openrouter_client or OpenRouterClient(
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url
        )

    def _resolve_workspace_context(
        self,
        project_id: Optional[str],
        session_id: str
    ) -> tuple[Optional[str], Path, dict[str, Any]]:
        """
        Resolves active project ID, workspace directory, and tool execution context.
        """
        resolved_project_id = project_id
        resolved_workspace_path = Path(settings.workspace_path).resolve()

        try:
            with self.memory_store._get_session() as db:
                from app.database.models import Project
                proj = None
                if resolved_project_id:
                    proj = db.get(Project, resolved_project_id)
                if not proj:
                    # Check for active project
                    stmt = select(Project).where(Project.is_active == True)
                    proj = db.exec(stmt).first()
                    if proj and isinstance(getattr(proj, "id", None), str):
                        resolved_project_id = proj.id

                # Guard against non-string values (e.g. mocked sessions in tests),
                # which would otherwise create junk directories on disk.
                proj_ws = getattr(proj, "workspace_path", None) if proj else None
                if isinstance(proj_ws, str) and proj_ws.strip():
                    resolved_workspace_path = Path(proj_ws).resolve()
        except Exception as e:
            logger.warning("Failed resolving project workspace from DB: %s", e)

        if not resolved_workspace_path.exists():
            try:
                resolved_workspace_path.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass

        tool_context = {
            "workspace_path": str(resolved_workspace_path),
            "project_id": resolved_project_id,
            "session_id": session_id,
        }

        return resolved_project_id, resolved_workspace_path, tool_context

    def _build_attachment_prompt(
        self,
        session_id: str,
        project_id: Optional[str] = None,
        direct_attachments: Optional[list[dict[str, Any]]] = None
    ) -> str:
        """
        Gathers attachments associated with the current session/project or passed directly,
        reads local text/code contents, and formats them into a context injection block for the LLM.
        """
        import os
        from pathlib import Path
        from sqlmodel import select
        from app.database import SessionLocal
        from app.database.models import Attachment

        attachments_to_process: list[dict[str, Any]] = []
        if direct_attachments:
            attachments_to_process.extend(direct_attachments)

        try:
            session_factory = getattr(self.memory_store, "_session_factory", SessionLocal)
            with session_factory() as session:
                stmt = select(Attachment).where(Attachment.session_id == session_id)

                if project_id:
                    stmt = stmt.where(Attachment.project_id == project_id)
                db_attachments = session.exec(stmt).all()
                for a in db_attachments:
                    if not any(d.get("id") == a.id or d.get("path") == a.path for d in attachments_to_process):
                        attachments_to_process.append({
                            "id": a.id,
                            "filename": a.filename,
                            "path": a.path,
                            "size_bytes": a.size_bytes,
                            "content_type": a.content_type
                        })
        except Exception as e:
            logger.warning("Error fetching attachments from database: %s", e)

        if not attachments_to_process:
            return ""

        sections = ["\n\n[USER ATTACHED FILES IN THIS CONVERSATION]"]
        text_exts = {
            ".txt", ".md", ".py", ".js", ".ts", ".tsx", ".json", ".yaml", ".yml",
            ".cpp", ".h", ".hpp", ".cs", ".ini", ".csv", ".html", ".css", ".svg"
        }

        for att in attachments_to_process:
            fname = att.get("filename") or "attached_file"
            fpath = att.get("path") or ""
            size = att.get("size_bytes", 0)

            p = Path(fpath)
            if not p.exists() and fpath:
                ws_cand = Path(settings.workspace_path).resolve() / fpath
                if ws_cand.exists():
                    p = ws_cand

            content_text = ""
            ext = os.path.splitext(fname)[1].lower()

            if p.exists() and p.is_file() and ext in text_exts and size <= 100 * 1024:
                try:
                    with open(p, "r", encoding="utf-8", errors="replace") as f:
                        raw = f.read(16000)
                        if len(raw) == 16000:
                            raw += "\n... [Content truncated at 16KB]"
                        content_text = raw
                except Exception as e:
                    content_text = f"[Could not read content: {e}]"

            if content_text:
                sections.append(
                    f"--- File: {fname} (Saved to disk at: {p.as_posix()}) ---\n"
                    f"```{ext.lstrip('.') or 'text'}\n{content_text}\n```"
                )
            else:
                sections.append(
                    f"--- File: {fname} (Saved to disk at: {p.as_posix() if p.exists() else fpath}, Size: {size} bytes) ---\n"
                    f"[Binary/Non-text or large file. Use 'read_file(file_path=\"{p.as_posix() if p.exists() else fpath}\")' to inspect or read this file.]"
                )

        sections.append("--------------------------------------------------\n")
        return "\n".join(sections)

    def _slot_has_vision(self, slot: str, provider: Optional[str]) -> bool:
        """
        Whether image attachments on this turn can be sent as image parts: only the local
        llama-server path knows about projectors, and only when the slot's model has one beside
        it on disk. A cloud/OpenRouter turn or a text-only model gets the file-path description.
        """
        if provider and provider != "llama_cpp":
            return False
        try:
            from app.agent.runtime_process_manager import get_runtime_process_manager
            return get_runtime_process_manager().has_vision(slot)
        except Exception as exc:
            logger.debug("Could not determine vision capability for slot %s: %s", slot, exc)
            return False

    def _synthesize_voice(self, text: str):

        """Synthesize and play voice output if voice is enabled and TTS engine is available."""
        if not self.voice_output_enabled or not text or not self.tts_engine:
            return
        try:
            clean_text, _ = extract_tool_calls_from_text(text)
            clean_text = persona_manager.shape_for_speech(clean_text or text)
            clean_text = self.tts_engine.sanitize_text(clean_text)
            if clean_text:
                audio_bytes = self.tts_engine.synthesize(clean_text)
                if audio_bytes:
                    play_audio(audio_bytes)
        except Exception as e:
            logger.warning("Error synthesizing or playing orchestrator voice output: %s", e)

    def _finalize_result(self, result: OrchestratorResult) -> OrchestratorResult:
        """Finalizes orchestrator result and plays audio if enabled."""
        if self.voice_output_enabled and result.status == "completed" and result.response:
            self._synthesize_voice(result.response)
        return result

    async def run(
        self,
        user_message: str,
        session_id: Optional[str] = None,
        project_id: Optional[str] = None,
        requested_mode: Optional[str] = None,
        requested_model: Optional[str] = None,
        system_prompt: Optional[str] = None,
        approved_action_ids: Optional[list[str]] = None,
        max_iterations: int = 5,
        compaction_threshold_override: Optional[int] = None,
        chat_mode: Optional[str] = "WORKSPACE",
        attachments: Optional[list[dict[str, Any]]] = None
    ) -> OrchestratorResult:
        stop_playback()
        active_session_id = session_id or "default"

        # Voice toggle commands
        lower_msg = user_message.strip().lower()
        if lower_msg in ("stop talking", "be quiet", "silence", "stop speech", "stop audio"):
            stop_playback()
            return OrchestratorResult(
                response="I have stopped speaking.",
                model="system",
                provider="system",
                session_id=active_session_id
            )
        if lower_msg in ("voice on", "enable voice", "turn voice on", "unmute voice"):
            self.voice_output_enabled = True
            return self._finalize_result(OrchestratorResult(
                response="Voice output is now enabled.",
                model="system",
                provider="system",
                session_id=active_session_id
            ))
        if lower_msg in ("voice off", "disable voice", "turn voice off", "mute voice"):
            self.voice_output_enabled = False
            stop_playback()
            return OrchestratorResult(
                response="Voice output is now disabled.",
                model="system",
                provider="system",
                session_id=active_session_id
            )

        res = await self._run_internal(
            user_message=user_message,
            session_id=session_id,
            project_id=project_id,
            requested_mode=requested_mode,
            requested_model=requested_model,
            system_prompt=system_prompt,
            approved_action_ids=approved_action_ids,
            max_iterations=max_iterations,
            compaction_threshold_override=compaction_threshold_override,
            chat_mode=chat_mode,
            attachments=attachments
        )
        return self._finalize_result(res)

    async def _run_internal(
        self,
        user_message: str,
        session_id: Optional[str] = None,
        project_id: Optional[str] = None,
        requested_mode: Optional[str] = None,
        requested_model: Optional[str] = None,
        system_prompt: Optional[str] = None,
        approved_action_ids: Optional[list[str]] = None,
        max_iterations: int = 5,
        compaction_threshold_override: Optional[int] = None,
        chat_mode: Optional[str] = "WORKSPACE",
        attachments: Optional[list[dict[str, Any]]] = None
    ) -> OrchestratorResult:
        active_session_id = session_id or "default"
        resolved_project_id, workspace_path, tool_context = self._resolve_workspace_context(project_id, active_session_id)
        session_data = self.memory_store.get_or_create_session(active_session_id, chat_mode=chat_mode, project_id=resolved_project_id)
        effective_mode_str = (chat_mode or session_data.get("chat_mode") or "WORKSPACE").upper()
        resolved_chat_mode = ChatMode.SYSTEM if effective_mode_str == "SYSTEM" else ChatMode.WORKSPACE

        # 1. Routing Decision made first (Amendment 2)
        decision = self.router.evaluate(
            message=user_message,
            requested_mode=requested_mode,
            requested_model=requested_model
        )
        is_fast = "fast" in (decision.model or "").lower() or decision.mode == "fast"
        resolved_ctx_tokens = settings.llama_ctx_size_fast if is_fast else settings.llama_ctx_size_main

        logger.info("Routing decision: mode='%s', provider='%s', model='%s', reason='%s'",
                    decision.mode, decision.provider, decision.model, decision.reason)

        # 2. Dynamic Skills Matching & Prompt Injection
        matched_skills = self.skills_loader.match_skills(user_message)
        active_skill_names = [s.name for s in matched_skills]
        if active_skill_names:
            logger.info("Active dynamic skills matched: %s", active_skill_names)

        skill_prompt_injection = self.skills_loader.build_skill_prompt_injection(matched_skills)
        base_system_prompt = (system_prompt or build_system_prompt()) + skill_prompt_injection

        # 3. Compaction Evaluation (Step 4C)
        history = self.memory_store.get_messages(active_session_id)
        current_turn = {"role": "user", "content": user_message}
        full_conversation = history + [current_turn]

        compacted_msgs, compaction_info = await self.compactor.compact(
            messages=full_conversation,
            client=self.provider,
            model=decision.model,
            threshold_override=compaction_threshold_override
        )
        if compaction_info:
            logger.info("Compaction applied to session '%s': %s", active_session_id, compaction_info)
            self.memory_store.replace_messages(active_session_id, compacted_msgs[:-1])
            self.memory_store.record_compaction(
                session_id=active_session_id,
                strategy=compaction_info["strategy"],
                tokens_before=compaction_info["tokens_before"],
                tokens_after=compaction_info["tokens_after"],
                details=json.dumps(compaction_info.get("details")) if isinstance(compaction_info.get("details"), dict) else compaction_info.get("details")
            )

        # 4. RAG Retrieval in WORKSPACE mode
        retrieved_chunks = []
        active_rag_project_id = resolved_project_id or project_id
        if effective_mode_str == "WORKSPACE" and active_rag_project_id:
            try:
                retrieved_chunks = self.retriever.retrieve(
                    project_id=active_rag_project_id,
                    query=user_message,
                    top_k=settings.context_tier3_max_chunks
                )
            except Exception as rag_err:
                logger.warning("RAG retrieval failed during execution: %s", rag_err)

        # 5. Strict Tier-Based Token Budgeting
        context_pkg = self.context_manager.build_context(
            session_id=active_session_id,
            user_message=user_message,
            system_prompt=base_system_prompt,
            project_id=active_rag_project_id,
            attachments=attachments,
            retrieved_chunks=retrieved_chunks,
            chat_mode=effective_mode_str,
            max_context_tokens=resolved_ctx_tokens,
            vision=self._slot_has_vision("fast" if is_fast else "main", decision.provider),
        )

        # 6. Dynamic Tool Aggregation
        mcp_tools = self.mcp_manager.get_tool_definitions()
        relevant_base_tools = get_relevant_tools(user_message, chat_mode=effective_mode_str, matched_skills=matched_skills)
        combined_tools = (relevant_base_tools + mcp_tools) if relevant_base_tools else []

        self.memory_store.append_message(active_session_id, role="user", content=user_message)

        # 7. Determine Profile (coding vs general)
        is_tool_or_coding = bool(combined_tools) or any(
            kw in user_message.lower() for kw in ["code", "file", "func", "def ", "class ", "test", "run", "script", "debug"]
        )
        profile = "coding" if is_tool_or_coding else "general"

        # 8. Dispatch: OpenRouter Cloud (if enabled)
        if decision.provider == "openrouter" and getattr(settings, "cloud_routing_enabled", False):
            try:
                openrouter_res = await self.openrouter_client.chat(
                    messages=context_pkg.messages,
                    model=decision.model
                )
                content = ""
                choices = openrouter_res.get("choices", [])
                if choices:
                    content = choices[0].get("message", {}).get("content", "")

                self.memory_store.append_message(active_session_id, role="assistant", content=content)

                return OrchestratorResult(
                    response=content,
                    model=decision.model,
                    provider="openrouter",
                    status="completed",
                    session_id=active_session_id,
                    route_reason=decision.reason,
                    fallback_used=False,
                    compaction_performed=compaction_info,
                    active_skills=active_skill_names
                )
            except Exception as e:
                logger.warning("OpenRouter dispatch failed (%s). Falling back to local provider.", e)
                fallback_provider = get_model_provider("llama_cpp")
                result = await self._run_provider_loop(
                    provider=fallback_provider,
                    session_id=active_session_id,
                    conversation_messages=context_pkg.messages,
                    tools=combined_tools,
                    model="main",
                    system_prompt=context_pkg.system_prompt,
                    approved_action_ids=approved_action_ids,
                    max_iterations=max_iterations,
                    route_reason=f"{decision.reason} [Fallback: OpenRouter failed ({e}), used local llama.cpp]",
                    chat_mode=resolved_chat_mode,
                    profile=profile,
                    workspace_path=workspace_path,
                    tool_context=tool_context
                )
                result.fallback_used = True
                result.compaction_performed = compaction_info
                result.active_skills = active_skill_names
                return result

        # 9. Dispatch: Local Primary Provider (llama.cpp) or Fallback (Ollama)
        target_provider = self.provider if isinstance(self.provider, _LegacyClientAdapter) else get_model_provider(decision.provider)
        try:
            result = await self._run_provider_loop(
                provider=target_provider,
                session_id=active_session_id,
                conversation_messages=context_pkg.messages,
                tools=combined_tools,
                model=decision.model,
                system_prompt=context_pkg.system_prompt,
                approved_action_ids=approved_action_ids,
                max_iterations=max_iterations,
                route_reason=decision.reason,
                chat_mode=resolved_chat_mode,
                profile=profile,
                workspace_path=workspace_path,
                tool_context=tool_context
            )
            result.compaction_performed = compaction_info
            result.active_skills = active_skill_names
            return result
        except Exception as e:
            if target_provider.name == "llama_cpp":
                logger.warning("Primary llama.cpp dispatch failed (%s). Falling back to Ollama.", e)
                try:
                    fallback_provider = get_model_provider("ollama")
                    fallback_model = settings.ollama_main_model
                    result = await self._run_provider_loop(
                        provider=fallback_provider,
                        session_id=active_session_id,
                        conversation_messages=context_pkg.messages,
                        tools=combined_tools,
                        model=fallback_model,
                        system_prompt=context_pkg.system_prompt,
                        approved_action_ids=approved_action_ids,
                        max_iterations=max_iterations,
                        route_reason=f"{decision.reason} [Fallback: llama.cpp failed ({e}), used local Ollama ({fallback_model})]",
                        chat_mode=resolved_chat_mode,
                        profile=profile,
                        workspace_path=workspace_path,
                        tool_context=tool_context
                    )
                    result.fallback_used = True
                    result.compaction_performed = compaction_info
                    result.active_skills = active_skill_names
                    return result
                except Exception as fallback_err:
                    logger.error("Fallback to Ollama also failed: %s", fallback_err)
                    raise e
            raise


    async def _run_provider_loop(
        self,
        provider: ModelProvider,
        session_id: str,
        conversation_messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,
        system_prompt: str,
        approved_action_ids: Optional[list[str]] = None,
        max_iterations: int = 5,
        route_reason: str = "Local provider execution",
        chat_mode: ChatMode = ChatMode.WORKSPACE,
        profile: str = "general",
        workspace_path: Optional[str | Path] = None,
        tool_context: Optional[dict[str, Any]] = None
    ) -> OrchestratorResult:
        """
        Execute deterministic agent loop against ModelProvider with schema validation,
        loop breaking, rate limits, and safety gating.
        """
        turn_id = f"turn_{uuid.uuid4().hex[:12]}"
        ws_root = Path(workspace_path or settings.workspace_path).resolve()
        tool_ctx = tool_context or {"workspace_path": str(ws_root), "session_id": session_id}
        call_history = CallHistory(window=3)
        total_turn_tool_calls = 0
        tool_turn_counts: dict[str, int] = {}
        repair_attempts: dict[str, int] = {}
        model_tier = "tier2" if provider.name == "llama_cpp" else "tier1"

        messages: list[dict[str, Any]] = []
        messages.append({"role": "system", "content": system_prompt})
        messages.extend(conversation_messages)

        tools_used: list[dict[str, Any]] = []

        for iteration in range(max_iterations):
            logger.info("%s loop iteration %d/%d for model '%s' (turn_id=%s)", provider.name, iteration + 1, max_iterations, model, turn_id)

            chat_response = await provider.chat(
                model=model,
                messages=messages,
                tools=tools if tools else None,
                profile=profile
            )

            message_obj = chat_response.get("message", {})
            content = message_obj.get("content", "") or ""
            tool_calls = message_obj.get("tool_calls")

            # Fallback tool call extraction from text if needed
            if not tool_calls:
                latest_user_prompt = ""
                for msg in reversed(messages):
                    if isinstance(msg, dict) and msg.get("role") == "user":
                        latest_user_prompt = msg.get("content", "")
                        break
                content, extracted_calls = extract_tool_calls_from_text(content, user_prompt=latest_user_prompt)
                if extracted_calls:
                    logger.info("Extracted %d tool call(s) from raw model text stream", len(extracted_calls))
                    tool_calls = extracted_calls

            if not tool_calls:
                if not content.strip():
                    logger.info("Model returned empty content with tools schema. Requesting text generation without tools parameter.")
                    synth_response = await provider.chat(
                        model=model,
                        messages=messages,
                        profile=profile
                    )
                    content = synth_response.get("message", {}).get("content", "") or ""

                logger.info("No further tool calls requested. Returning final %s response.", provider.name)
                self.memory_store.append_message(session_id, role="assistant", content=content)
                return OrchestratorResult(
                    response=content,
                    model=model,
                    provider=provider.name,
                    status="completed",
                    session_id=session_id,
                    route_reason=route_reason,
                    fallback_used=False,
                    tools_used=tools_used
                )

            logger.info("%s requested %d tool call(s)", provider.name, len(tool_calls))

            # Normalize tool calls
            normalized_tool_calls = []
            for tc in tool_calls:
                func_data = tc.get("function", {})
                fn_name = func_data.get("name", "")
                fn_args = func_data.get("arguments", {})
                normalized_tool_calls.append({
                    "id": tc.get("id", f"call_{uuid.uuid4().hex[:8]}"),
                    "name": fn_name,
                    "args": fn_args if isinstance(fn_args, dict) else {}
                })

            # Duplicate / Loop breaker
            duplicate_detected = False
            for tc in normalized_tool_calls:
                if call_history.is_duplicate(tc["name"], tc["args"]):
                    duplicate_detected = True
                    break
            if duplicate_detected:
                loop_msg = "Jarvis attempted the same action twice — stopping to avoid a loop."
                logger.warning("Duplicate tool invocation loop detected in %s turn. Halting turn.", provider.name)
                self.memory_store.append_message(session_id, role="assistant", content=loop_msg)
                return OrchestratorResult(
                    response=loop_msg,
                    model=model,
                    provider=provider.name,
                    status="completed",
                    session_id=session_id,
                    route_reason=route_reason,
                    fallback_used=False,
                    tools_used=tools_used
                )

            for tc in normalized_tool_calls:
                call_history.record(tc["name"], tc["args"])

            # Per-Tool Rate Limiting
            rate_limited_tool = None
            for tc in normalized_tool_calls:
                fn_name = tc["name"]
                current_count = tool_turn_counts.get(fn_name, 0)
                if not check_rate_limit(fn_name, current_count):
                    rate_limited_tool = fn_name
                    break

            if rate_limited_tool:
                limit = RATE_LIMITS.get(rate_limited_tool, 0)
                rate_msg = f"Rate limit exceeded: Tool '{rate_limited_tool}' reached the maximum allowed limit of {limit} calls for this turn."
                logger.warning(rate_msg)
                self.memory_store.append_message(session_id, role="assistant", content=rate_msg)
                return OrchestratorResult(
                    response=rate_msg,
                    model=model,
                    provider=provider.name,
                    status="completed",
                    session_id=session_id,
                    route_reason=route_reason,
                    fallback_used=False,
                    tools_used=tools_used
                )

            # Global per-turn cap
            if total_turn_tool_calls + len(normalized_tool_calls) > MAX_TOOL_CALLS_PER_TURN:
                cap_msg = f"Turn tool call limit exceeded: Reached maximum allowed {MAX_TOOL_CALLS_PER_TURN} calls for this turn. Halting further tool execution for safety."
                logger.warning("Turn tool call cap reached (%d / %d). Halting turn.", total_turn_tool_calls, MAX_TOOL_CALLS_PER_TURN)
                self.memory_store.append_message(session_id, role="assistant", content=cap_msg)
                return OrchestratorResult(
                    response=cap_msg,
                    model=model,
                    provider=provider.name,
                    status="completed",
                    session_id=session_id,
                    route_reason=route_reason,
                    fallback_used=False,
                    tools_used=tools_used
                )

            # Schema validation & Repair-then-Escalate
            validation_failed_items = []
            validated_tool_calls = []

            for tc in normalized_tool_calls:
                fn_name = tc["name"]
                fn_args = tc["args"]
                call_id = tc["id"]
                schema = get_tool_schema(fn_name)
                current_attempt = repair_attempts.get(fn_name, 0)

                val_res = await validate_tool_call(
                    tool_name=fn_name,
                    raw_args=fn_args,
                    schema=schema,
                    repair_attempt=current_attempt
                )

                if not val_res.valid:
                    if current_attempt == 0:
                        repair_attempts[fn_name] = 1
                        self.memory_store.record_tool_call_audit(
                            call_id=call_id,
                            turn_id=turn_id,
                            tool_name=fn_name,
                            args=fn_args,
                            model_tier=model_tier,
                            validation_result="invalid_repaired",
                            permission_result="blocked",
                            executed=False,
                            error=val_res.error,
                            repair_attempt=0
                        )
                        validation_failed_items.append((fn_name, fn_args, val_res.error, call_id))
                    else:
                        self.memory_store.record_tool_call_audit(
                            call_id=call_id,
                            turn_id=turn_id,
                            tool_name=fn_name,
                            args=fn_args,
                            model_tier=model_tier,
                            validation_result="invalid_escalated",
                            permission_result="blocked",
                            executed=False,
                            error=val_res.error,
                            repair_attempt=1
                        )
                        self.reliability_monitor.evaluate_and_trigger_rollback(model_tier=model_tier)
                        if self.openrouter_client and (getattr(self.openrouter_client, "is_configured", False) or hasattr(self.openrouter_client, "chat")):
                            logger.warning("Tool call validation failed twice. Escalating remaining turn to Tier 3 (OpenRouter).")
                            try:
                                openrouter_res = await self.openrouter_client.chat(
                                    messages=messages,
                                    model=settings.openrouter_heavy_model
                                )
                                esc_content = ""
                                choices = openrouter_res.get("choices", []) if isinstance(openrouter_res, dict) else []
                                if choices:
                                    esc_content = choices[0].get("message", {}).get("content", "")
                                elif isinstance(openrouter_res, dict) and "message" in openrouter_res:
                                    esc_content = openrouter_res["message"].get("content", "")
                                self.memory_store.append_message(session_id, role="assistant", content=esc_content)
                                return OrchestratorResult(
                                    response=esc_content,
                                    model=settings.openrouter_heavy_model,
                                    provider="openrouter",
                                    status="completed",
                                    session_id=session_id,
                                    route_reason=f"{route_reason} [Escalated to Tier 3 OpenRouter due to repeated tool argument validation failures]",
                                    fallback_used=False,
                                    tools_used=tools_used
                                )
                            except Exception as e:
                                logger.error("OpenRouter Tier 3 escalation failed: %s", e)
                        validation_failed_items.append((fn_name, fn_args, val_res.error, call_id))
                else:
                    validated_tool_calls.append((call_id, fn_name, val_res.args))

            if validation_failed_items and not validated_tool_calls:
                assistant_msg = {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": [
                        {
                            "id": item[3],
                            "type": "function",
                            "function": {"name": item[0], "arguments": item[1]}
                        }
                        for item in validation_failed_items
                    ]
                }
                messages.append(assistant_msg)
                for fn_name, fn_args, err_str, cid in validation_failed_items:
                    repair_msg = f"Schema validation error for '{fn_name}': {err_str}. Please correct the parameters and retry."
                    messages.append({
                        "role": "tool",
                        "tool_call_id": cid,
                        "name": fn_name,
                        "content": repair_msg
                    })
                continue

            # Safety permission checks
            batch_calls = [{"name": name, "args": args} for _, name, args in validated_tool_calls]
            batch_permission = evaluate_tool_calls_batch(
                tool_calls=batch_calls,
                chat_mode=chat_mode,
                approved_action_ids=approved_action_ids,
                workspace_path=str(ws_root)
            )

            if not batch_permission.all_allowed:
                pending_list = [
                    {
                        "action_id": p.action_id,
                        "tool": p.tool,
                        "args": p.args,
                        "risk_tier": p.risk_tier.value,
                        "reason": p.reason
                    }
                    for p in batch_permission.pending_confirmations
                ]
                
                for call_id, name, args in validated_tool_calls:
                    self.memory_store.record_tool_call_audit(
                        call_id=call_id,
                        turn_id=turn_id,
                        tool_name=name,
                        args=args,
                        model_tier=model_tier,
                        validation_result="valid",
                        permission_result="blocked",
                        executed=False,
                        error="Action requires user confirmation",
                        repair_attempt=repair_attempts.get(name, 0)
                    )

                logger.warning(
                    "Tool execution blocked by safety permission gate. %d action(s) require confirmation. Details: %s",
                    len(pending_list), pending_list
                )
                prompt = build_confirmation_prompt(pending_list, persona_manager.get_active())

                return OrchestratorResult(
                    response=prompt["text"],
                    model=model,
                    provider=provider.name,
                    status="confirmation_required",
                    session_id=session_id,
                    route_reason=route_reason,
                    fallback_used=False,
                    tools_used=tools_used,
                    pending_confirmations=pending_list,
                    spoken=prompt["spoken"],
                )

            # All tools approved -> append assistant message
            assistant_msg = {
                "role": "assistant",
                "content": content,
                "tool_calls": [
                    {
                        "id": cid,
                        "type": "function",
                        "function": {"name": name, "arguments": args}
                    }
                    for cid, name, args in validated_tool_calls
                ]
            }
            messages.append(assistant_msg)

            # Execute tools & Record audit log
            for call_id, fn_name, fn_args in validated_tool_calls:
                total_turn_tool_calls += 1
                tool_turn_counts[fn_name] = tool_turn_counts.get(fn_name, 0) + 1

                if self.mcp_manager.is_mcp_tool(fn_name):
                    logger.info("Executing MCP tool '%s'", fn_name)
                    tool_output = await self.mcp_manager.call_tool(fn_name, fn_args)
                else:
                    logger.info("Executing Native tool '%s'", fn_name)
                    tool_output = execute_tool(fn_name, fn_args, context=tool_ctx)

                self.memory_store.record_tool_call_audit(
                    call_id=call_id,
                    turn_id=turn_id,
                    tool_name=fn_name,
                    args=fn_args,
                    model_tier=model_tier,
                    validation_result="valid",
                    permission_result="allowed",
                    executed=True,
                    error=None,
                    repair_attempt=repair_attempts.get(fn_name, 0)
                )
                self.reliability_monitor.evaluate_and_trigger_rollback(model_tier=model_tier)

                tools_used.append({
                    "tool": fn_name,
                    "args": fn_args,
                    "result": tool_output
                })

                messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": fn_name,
                    "content": tool_output
                })
                self.memory_store.append_message(session_id, role="tool", content=tool_output, name=fn_name)

        logger.warning("Max tool iterations reached (%d). Requesting final summary from %s.", max_iterations, provider.name)
        final_response = await provider.chat(
            model=model,
            messages=messages,
            profile=profile
        )
        final_content = final_response.get("message", {}).get("content", "") or ""
        self.memory_store.append_message(session_id, role="assistant", content=final_content)

        return OrchestratorResult(
            response=final_content,
            model=model,
            provider=provider.name,
            status="completed",
            session_id=session_id,
            route_reason=route_reason,
            fallback_used=False,
            tools_used=tools_used
        )

    async def run_stream(
        self,
        user_message: str,
        session_id: Optional[str] = None,
        project_id: Optional[str] = None,
        requested_mode: Optional[str] = None,
        requested_model: Optional[str] = None,
        system_prompt: Optional[str] = None,
        approved_action_ids: Optional[list[str]] = None,
        max_iterations: int = 5,
        compaction_threshold_override: Optional[int] = None,
        chat_mode: Optional[str] = "WORKSPACE",
        attachments: Optional[list[dict[str, Any]]] = None
    ):
        """
        Asynchronously streams chat tokens, tool execution events, and metadata.
        Yields JSON event dictionaries: {"event": "...", "data": {...}}
        """
        stop_playback()
        active_session_id = session_id or "default"

        # Conversational voice toggle commands
        lower_msg = user_message.strip().lower()
        if lower_msg in ("stop talking", "be quiet", "silence", "stop speech", "stop audio"):
            stop_playback()
            yield {"event": "done", "data": {"response": "I have stopped speaking.", "model": "system", "provider": "system", "session_id": active_session_id}}
            return
        if lower_msg in ("voice on", "enable voice", "turn voice on", "unmute voice"):
            self.voice_output_enabled = True
            msg = "Voice output is now enabled."
            yield {"event": "done", "data": {"response": msg, "model": "system", "provider": "system", "session_id": active_session_id}}
            self._synthesize_voice(msg)
            return
        if lower_msg in ("voice off", "disable voice", "turn voice off", "mute voice"):
            self.voice_output_enabled = False
            stop_playback()
            yield {"event": "done", "data": {"response": "Voice output is now disabled.", "model": "system", "provider": "system", "session_id": active_session_id}}
            return

        active_session_id = session_id or "default"
        resolved_project_id, workspace_path, tool_context = self._resolve_workspace_context(project_id, active_session_id)
        session_data = self.memory_store.get_or_create_session(active_session_id, chat_mode=chat_mode, project_id=resolved_project_id)
        effective_mode_str = (chat_mode or session_data.get("chat_mode") or "WORKSPACE").upper()
        resolved_chat_mode = ChatMode.SYSTEM if effective_mode_str == "SYSTEM" else ChatMode.WORKSPACE

        # 1. Model routing decision made first (Amendment 2)
        decision = self.router.evaluate(
            message=user_message,
            requested_mode=requested_mode,
            requested_model=requested_model
        )
        is_fast = "fast" in (decision.model or "").lower() or decision.mode == "fast"
        resolved_ctx_tokens = settings.llama_ctx_size_fast if is_fast else settings.llama_ctx_size_main

        # 2. Match skills & prepare base prompts
        matched_skills = self.skills_loader.match_skills(user_message)
        active_skill_names = [s.name for s in matched_skills]
        skill_prompt_injection = self.skills_loader.build_skill_prompt_injection(matched_skills)
        base_system_prompt = (system_prompt or build_system_prompt()) + skill_prompt_injection

        # 3. RAG Retrieval in WORKSPACE mode
        retrieved_chunks = []
        active_rag_project_id = resolved_project_id or project_id
        if effective_mode_str == "WORKSPACE" and active_rag_project_id:
            try:
                retrieved_chunks = self.retriever.retrieve(
                    project_id=active_rag_project_id,
                    query=user_message,
                    top_k=settings.context_tier3_max_chunks
                )
            except Exception as rag_err:
                logger.warning("RAG retrieval failed during stream setup: %s", rag_err)

        # 4. Strict Tier-Based Token Budgeting
        context_pkg = self.context_manager.build_context(
            session_id=active_session_id,
            user_message=user_message,
            system_prompt=base_system_prompt,
            project_id=active_rag_project_id,
            attachments=attachments,
            retrieved_chunks=retrieved_chunks,
            chat_mode=effective_mode_str,
            max_context_tokens=resolved_ctx_tokens,
            vision=self._slot_has_vision("fast" if is_fast else "main", decision.provider),
        )

        # 5. Emit retrieval_context SSE event for UI observability
        yield {
            "event": "retrieval_context",
            "data": {
                "chunks_used": context_pkg.retrieved_chunks_used,
                "chunks_dropped": context_pkg.retrieved_chunks_dropped,
                "budget_report": context_pkg.budget_report
            }
        }

        mcp_tools = self.mcp_manager.get_tool_definitions()
        relevant_base_tools = get_relevant_tools(user_message, chat_mode=effective_mode_str, matched_skills=matched_skills)
        combined_tools = (relevant_base_tools + mcp_tools) if relevant_base_tools else []

        self.memory_store.append_message(active_session_id, role="user", content=user_message)

        is_tool_or_coding = bool(combined_tools) or any(
            kw in user_message.lower() for kw in ["code", "file", "func", "def ", "class ", "test", "run", "script", "debug"]
        )
        profile = "coding" if is_tool_or_coding else "general"

        if decision.provider == "openrouter" and getattr(settings, "cloud_routing_enabled", False):
            async for ev in self._run_openrouter_stream_loop(
                session_id=active_session_id,
                conversation_messages=context_pkg.messages,
                model=decision.model,
                system_prompt=context_pkg.system_prompt,
                route_reason=decision.reason,
                active_skills=active_skill_names,
                compaction_info=None
            ):
                yield ev
            return

        target_provider = self.provider if isinstance(self.provider, _LegacyClientAdapter) else get_model_provider(decision.provider)
        async for ev in self._run_provider_stream_loop(
            provider=target_provider,
            session_id=active_session_id,
            conversation_messages=context_pkg.messages,
            tools=combined_tools,
            model=decision.model,
            system_prompt=context_pkg.system_prompt,
            approved_action_ids=approved_action_ids,
            max_iterations=max_iterations,
            route_reason=decision.reason,
            chat_mode=resolved_chat_mode,
            profile=profile,
            active_skills=active_skill_names,
            compaction_info=None,
            workspace_path=workspace_path,
            tool_context=tool_context
        ):
            yield ev


    async def _run_provider_stream_loop(
        self,
        provider: ModelProvider,
        session_id: str,
        conversation_messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,
        system_prompt: str,
        approved_action_ids: Optional[list[str]] = None,
        max_iterations: int = 5,
        route_reason: str = "Local provider execution",
        chat_mode: ChatMode = ChatMode.WORKSPACE,
        profile: str = "general",
        active_skills: Optional[list[str]] = None,
        compaction_info: Optional[dict] = None,
        workspace_path: Optional[str | Path] = None,
        tool_context: Optional[dict[str, Any]] = None
    ):
        ws_root = Path(workspace_path or settings.workspace_path).resolve()
        tool_ctx = tool_context or {"workspace_path": str(ws_root), "session_id": session_id}
        call_history = CallHistory(window=3)
        messages: list[dict[str, Any]] = []
        messages.append({"role": "system", "content": system_prompt})
        messages.extend(conversation_messages)

        tools_used: list[dict[str, Any]] = []

        for iteration in range(max_iterations):
            stream_gen = provider.stream_chat(
                model=model,
                messages=messages,
                tools=tools if tools else None,
                profile=profile
            )

            done_event = None
            accumulated_tool_calls = []
            accumulated_reasoning: list[str] = []

            async for ev in stream_gen:
                event_type = ev.get("event")
                if event_type == "text_delta":
                    yield {"event": "token", "data": {"delta": ev.get("content", "")}}
                elif event_type == "reasoning_delta":
                    accumulated_reasoning.append(ev.get("content", ""))
                    yield {"event": "reasoning", "data": {"delta": ev.get("content", "")}}
                elif event_type == "tool_draft":
                    yield {"event": "tool_draft", "data": {"tool": ev.get("tool", "tool"), "args_delta": ev.get("args_delta", "")}}
                elif event_type == "tool_call":
                    accumulated_tool_calls.append(ev.get("tool_call"))
                elif event_type == "done":
                    done_event = ev
                elif event_type == "error":
                    yield {"event": "error", "data": {"error": ev.get("message", "Stream error")}}
                    return

            raw_done = done_event.get("raw", {}) if done_event else {}
            content = raw_done.get("content", "") or ""
            tool_calls = accumulated_tool_calls or raw_done.get("tool_calls")
            full_reasoning = "".join(accumulated_reasoning) or raw_done.get("reasoning", "") or ""

            if not tool_calls:
                latest_user_prompt = ""
                for msg in reversed(messages):
                    if isinstance(msg, dict) and msg.get("role") == "user":
                        latest_user_prompt = msg.get("content", "")
                        break
                cleaned_content, extracted_calls = extract_tool_calls_from_text(content, user_prompt=latest_user_prompt)
                if extracted_calls:
                    tool_calls = extracted_calls
                    content = cleaned_content

            if not tool_calls:
                if not content.strip():
                    synth_response = await provider.chat(model=model, messages=messages, profile=profile)
                    content = synth_response.get("message", {}).get("content", "") or ""
                    yield {"event": "token", "data": {"delta": content}}

                self.memory_store.append_message(
                    session_id, role="assistant", content=content, reasoning_content=full_reasoning or None
                )
                self._synthesize_voice(content)
                yield {
                    "event": "done",
                    "data": {
                        "response": content,
                        "reasoning": full_reasoning,
                        "model": model,
                        "provider": provider.name,
                        "status": "completed",
                        "session_id": session_id,
                        "route_reason": route_reason,
                        "fallback_used": False,
                        "compaction_performed": compaction_info,
                        "active_skills": active_skills or [],
                        "tools_used": tools_used
                    }
                }
                return

            normalized_tool_calls = []
            for tc in tool_calls:
                func_data = tc.get("function", {})
                fn_name = func_data.get("name", "")
                fn_args = func_data.get("arguments", {})
                normalized_tool_calls.append({
                    "id": tc.get("id", f"call_{uuid.uuid4().hex[:8]}"),
                    "name": fn_name,
                    "args": fn_args if isinstance(fn_args, dict) else {}
                })

            duplicate_detected = False
            for tc in normalized_tool_calls:
                if call_history.is_duplicate(tc["name"], tc["args"]):
                    duplicate_detected = True
                    break
            if duplicate_detected:
                loop_msg = "Jarvis stopped executing to avoid a duplicate action loop."
                yield {"event": "token", "data": {"delta": loop_msg}}
                self.memory_store.append_message(session_id, role="assistant", content=loop_msg)
                yield {
                    "event": "done",
                    "data": {
                        "response": loop_msg,
                        "model": model,
                        "provider": provider.name,
                        "status": "completed",
                        "session_id": session_id,
                        "route_reason": route_reason,
                        "fallback_used": False,
                        "tools_used": tools_used
                    }
                }
                return

            batch_result = evaluate_tool_calls_batch(
                tool_calls=[{"name": tc["name"], "args": tc["args"]} for tc in normalized_tool_calls],
                approved_action_ids=approved_action_ids,
                chat_mode=chat_mode,
                workspace_path=str(ws_root)
            )

            if not batch_result.all_allowed:
                pending_list = [
                    {
                        "action_id": p.action_id,
                        "tool": p.tool,
                        "args": p.args,
                        "risk_tier": p.risk_tier.value,
                        "reason": p.reason
                    }
                    for p in batch_result.pending_confirmations
                ]
                prompt = build_confirmation_prompt(pending_list, persona_manager.get_active())
                yield {
                    "event": "confirmation_required",
                    "data": {
                        "status": "confirmation_required",
                        "pending_confirmations": pending_list,
                        "session_id": session_id,
                        "model": model,
                        "provider": provider.name,
                        "response": prompt["text"],
                        "spoken": prompt["spoken"],
                    }
                }
                return

            messages.append({"role": "assistant", "content": content or "", "tool_calls": tool_calls})
            for tc in normalized_tool_calls:
                t_name = tc["name"]
                t_args = tc["args"]
                t_id = tc["id"]

                call_history.record(t_name, t_args)
                yield {"event": "tool_start", "data": {"tool": t_name, "args": t_args}}

                schema = get_tool_schema(t_name)
                val_result = await validate_tool_call(
                    tool_name=t_name,
                    raw_args=t_args,
                    schema=schema,
                    repair_attempt=0
                )
                if not val_result.valid:
                    result_str = f"Validation Error for {t_name}: {val_result.error}"
                    is_ok = False
                else:
                    try:
                        if self.mcp_manager.is_mcp_tool(t_name):
                            result_str = await self.mcp_manager.call_tool(t_name, val_result.args or t_args)
                        else:
                            result_str = execute_tool(t_name, val_result.args or t_args, context=tool_ctx)
                        is_ok = not str(result_str).startswith("Error")
                    except Exception as ex:
                        result_str = f"Error executing {t_name}: {ex}"
                        is_ok = False

                tool_item = {
                    "tool": t_name,
                    "args": t_args,
                    "status": "success" if is_ok else "error",
                    "result": result_str[:2000] if isinstance(result_str, str) else result_str
                }
                tools_used.append(tool_item)
                yield {"event": "tool_end", "data": tool_item}

                messages.append({
                    "role": "tool",
                    "tool_call_id": t_id,
                    "name": t_name,
                    "content": result_str
                })

    async def _run_openrouter_stream_loop(
        self,
        session_id: str,
        conversation_messages: list[dict[str, Any]],
        model: str,
        system_prompt: str,
        route_reason: str = "Cloud OpenRouter execution",
        active_skills: Optional[list[str]] = None,
        compaction_info: Optional[dict] = None
    ):
        messages = [{"role": "system", "content": system_prompt}] + conversation_messages
        stream_gen = self.openrouter_client.chat_stream(messages=messages, model=model)

        done_event = None
        async for ev in stream_gen:
            if ev.get("type") == "token":
                yield {"event": "token", "data": {"delta": ev["delta"]}}
            elif ev.get("type") == "done":
                done_event = ev

        content = done_event.get("content", "") if done_event else ""
        self.memory_store.append_message(session_id, role="assistant", content=content)
        self._synthesize_voice(content)
        yield {
            "event": "done",
            "data": {
                "response": content,
                "model": model,
                "provider": "openrouter",
                "status": "completed",
                "session_id": session_id,
                "route_reason": route_reason,
                "fallback_used": False,
                "compaction_performed": compaction_info,
                "active_skills": active_skills or [],
                "tools_used": []
            }
        }
