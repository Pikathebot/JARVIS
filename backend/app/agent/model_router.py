import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Union
from app.config import settings

logger = logging.getLogger("jarvis.agent.router")


class RoutingMode(str, Enum):
    AUTO = "auto"
    NORMAL = "normal"
    HEAVY = "heavy"


class TaskType(str, Enum):
    """
    Task classification for deterministic model routing (Build Plan Section 22).
    - simple_chat, summarization, background_memory, compaction -> the "fast" slot
    - deep_reasoning, coding, complex_tool_use -> the "main" slot
    """
    SIMPLE_CHAT = "simple_chat"
    SUMMARIZATION = "summarization"
    BACKGROUND_MEMORY = "background_memory"
    COMPACTION = "compaction"
    DEEP_REASONING = "deep_reasoning"
    CODING = "coding"
    COMPLEX_TOOL_USE = "complex_tool_use"
    VISION = "vision"
    WEB_EXTRACTION = "web_extraction"


# Patterns indicating coding or tool-heavy tasks requiring the MAIN model
CODING_AND_TOOL_PATTERNS = [
    re.compile(r"\b(def |class |import |function|async |return |const |let |var |struct |impl )\b"),
    re.compile(r"\b(write_file|patch_file|read_file|grep_in_files|find_files|execute_command)\b"),
    re.compile(r"\b(refactor|debug|compile|pytest|unittest|traceback|syntaxerror|exception)\b", re.IGNORECASE),
    re.compile(r"\b(codebase|repository|script|algorithm|regex|api endpoint|backend|frontend)\b", re.IGNORECASE),
]

# Concrete regex patterns indicating high task complexity
HEAVY_TASK_PATTERNS = [
    re.compile(r"\b(system\s+design|architect(?:ure|ural)?\s+design|distributed\s+systems?)\b", re.IGNORECASE),
    re.compile(r"\b(microservices?\s+architecture|high-availability\s+architecture)\b", re.IGNORECASE),
    re.compile(r"\b(formal\s+verification|mathematical\s+proof|theorem\s+proving)\b", re.IGNORECASE),
    re.compile(r"\b(full\s+codebase\s+refactor(?:ing)?|architectural\s+refactoring)\b", re.IGNORECASE),
]

# Explicit user prompt prefix tags
HEAVY_TAG_PREFIXES = (
    "[heavy]",
    "/heavy",
    "heavy mode:",
    "deep reason:",
    "deep reasoning:",
    "think step by step in detail:",
)


@dataclass
class RoutingDecision:
    mode: str  # "normal" | "heavy"
    provider: str  # "llama_cpp" | "openrouter"
    model: str
    reason: str


class ModelRouter:
    """
    Routes execution to the local llama.cpp slots ("main" / "fast" -- which GGUF each slot
    serves is chosen at runtime, see model_catalog) or to OpenRouter Heavy Mode, which is
    disabled by default.
    """

    def __init__(
        self,
        default_mode: str = "auto",
        openrouter_heavy_model: Optional[str] = None,
    ):
        self.default_mode = default_mode
        self.openrouter_heavy_model = openrouter_heavy_model or settings.openrouter_heavy_model

    def _resolve_local_target(self, requested_model: Optional[str] = None, prefer_fast: bool = False) -> tuple[str, str, str]:
        """
        The local llama.cpp slot for this turn. Returns (provider, model, label), where model is a
        slot name ("main" / "fast") unless the caller named something else explicitly.
        """
        provider = "llama_cpp"
        if requested_model:
            req_lower = requested_model.lower().strip()
            if req_lower in ("fast", "4b", "qwen3.5-4b"):
                return provider, "fast", "llama.cpp (fast slot)"
            if req_lower in ("main", "9b", "qwen3.5-9b", "default"):
                return provider, "main", "llama.cpp (main slot)"
            return provider, requested_model, f"llama.cpp ({requested_model})"
        if prefer_fast:
            return provider, "fast", "llama.cpp (fast slot)"
        return provider, "main", "llama.cpp (main slot)"

    def route_task(
        self,
        task_type: Union[TaskType, str],
        message: str = "",
        requested_mode: Optional[str] = None,
        requested_model: Optional[str] = None,
    ) -> RoutingDecision:
        """
        Deterministic routing based on TaskType (Build Plan Section 22):
        - simple_chat, summarization, background_memory, compaction -> FAST_MODEL
        - deep_reasoning, coding, complex_tool_use -> MAIN_MODEL
        """
        task_str = task_type.value if isinstance(task_type, TaskType) else str(task_type).lower().strip()

        # 1. Vision Task Type -> Route to VISION_MODEL
        if task_str in (TaskType.VISION.value, "vision"):
            vision_target = requested_model or getattr(settings, "vision_model", None) or "Qwen2.5-VL-7B-Instruct"
            decision = RoutingDecision(
                mode="normal",
                provider="llama_cpp",
                model=vision_target,
                reason=f"Task '{task_str}' routed deterministically to VISION_MODEL ({vision_target})."
            )
            logger.info("[ModelRouter] %s", decision.reason)
            return decision

        fast_tasks = {
            TaskType.SIMPLE_CHAT.value,
            TaskType.SUMMARIZATION.value,
            TaskType.BACKGROUND_MEMORY.value,
            TaskType.COMPACTION.value,
            TaskType.WEB_EXTRACTION.value,
            "simple_chat",
            "summarization",
            "background_memory",
            "compaction",
            "web_extraction",
        }

        prefer_fast = task_str in fast_tasks
        provider, model, label = self._resolve_local_target(requested_model, prefer_fast=prefer_fast)
        tier_label = "FAST_MODEL" if prefer_fast else "MAIN_MODEL"

        decision = RoutingDecision(
            mode="normal",
            provider=provider,
            model=model,
            reason=f"Task '{task_str}' routed deterministically to {tier_label} ({label})."
        )

        logger.info(
            "[ModelRouter] Deterministic route: task_type='%s' -> target='%s' via '%s' (Reason: %s)",
            task_str, decision.model, decision.provider, decision.reason
        )
        return decision

    def evaluate(
        self,
        message: str,
        requested_mode: Optional[str] = None,
        requested_model: Optional[str] = None,
        task_type: Optional[Union[TaskType, str]] = None,
    ) -> RoutingDecision:
        """
        Evaluate user message and request parameters to produce a deterministic RoutingDecision.
        Maintains complete backward compatibility with Phase 4 agent loop callers.
        """
        if task_type:
            return self.route_task(task_type, message, requested_mode, requested_model)

        mode_str = (requested_mode or self.default_mode).lower().strip()
        msg_clean = message.strip()
        cloud_enabled = getattr(settings, "cloud_routing_enabled", False) and getattr(settings, "openrouter_enabled", False)

        # 1. Explicit Mode: HEAVY
        if mode_str == RoutingMode.HEAVY.value:
            if cloud_enabled:
                target_model = requested_model or self.openrouter_heavy_model
                decision = RoutingDecision(
                    mode="heavy",
                    provider="openrouter",
                    model=target_model,
                    reason="Explicitly requested Heavy Mode (Cloud OpenRouter) via request parameters."
                )
                logger.info("[ModelRouter] %s", decision.reason)
                return decision
            else:
                provider, model, label = self._resolve_local_target(requested_model, prefer_fast=False)
                decision = RoutingDecision(
                    mode="normal",
                    provider=provider,
                    model=model,
                    reason=f"Heavy Mode requested but cloud routing is disabled by config; routed to local {label}."
                )
                logger.info("[ModelRouter] %s", decision.reason)
                return decision

        # 2. Explicit Mode: NORMAL
        if mode_str == RoutingMode.NORMAL.value:
            provider, model, label = self._resolve_local_target(requested_model, prefer_fast=False)
            decision = RoutingDecision(
                mode="normal",
                provider=provider,
                model=model,
                reason=f"Explicitly requested Normal Mode ({label}) via request parameters."
            )
            logger.info("[ModelRouter] %s", decision.reason)
            return decision

        # 3. AUTO Mode: Check prompt tags
        msg_lower = msg_clean.lower()
        for tag in HEAVY_TAG_PREFIXES:
            if msg_lower.startswith(tag):
                if cloud_enabled:
                    target_model = requested_model or self.openrouter_heavy_model
                    decision = RoutingDecision(
                        mode="heavy",
                        provider="openrouter",
                        model=target_model,
                        reason=f"Matched explicit user heavy-mode trigger tag: '{tag}'."
                    )
                    logger.info("[ModelRouter] %s", decision.reason)
                    return decision
                else:
                    provider, model, label = self._resolve_local_target(requested_model, prefer_fast=False)
                    decision = RoutingDecision(
                        mode="normal",
                        provider=provider,
                        model=model,
                        reason=f"Matched heavy trigger tag '{tag}', but cloud routing is disabled; routed to local {label}."
                    )
                    logger.info("[ModelRouter] %s", decision.reason)
                    return decision

        # 4. AUTO Mode: Check heavy complexity heuristics
        for pattern in HEAVY_TASK_PATTERNS:
            match = pattern.search(msg_clean)
            if match:
                if cloud_enabled:
                    target_model = requested_model or self.openrouter_heavy_model
                    decision = RoutingDecision(
                        mode="heavy",
                        provider="openrouter",
                        model=target_model,
                        reason=f"Matched complex reasoning task pattern: '{match.group(0)}'."
                    )
                    logger.info("[ModelRouter] %s", decision.reason)
                    return decision
                else:
                    provider, model, label = self._resolve_local_target(requested_model, prefer_fast=False)
                    decision = RoutingDecision(
                        mode="normal",
                        provider=provider,
                        model=model,
                        reason=f"Matched complex task pattern '{match.group(0)}' (local {label})."
                    )
                    logger.info("[ModelRouter] %s", decision.reason)
                    return decision

        # 5. Default AUTO Mode: Check coding/tools vs fast query
        is_coding_task = any(p.search(msg_clean) for p in CODING_AND_TOOL_PATTERNS)
        prefer_fast = not is_coding_task and len(msg_clean.split()) <= 15 and not any(kw in msg_lower for kw in ["tool", "file", "search", "run", "execute", "create", "write"])

        provider, model, label = self._resolve_local_target(requested_model, prefer_fast=prefer_fast)
        decision = RoutingDecision(
            mode="normal",
            provider=provider,
            model=model,
            reason=f"Local execution routed to {label}."
        )
        logger.info("[ModelRouter] %s", decision.reason)
        return decision
