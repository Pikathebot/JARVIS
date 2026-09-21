import copy
import json
import logging
import re
from typing import Any, Optional, Union
from pydantic import BaseModel, Field

from app.config import settings
from app.memory.store import MemoryStore
from app.memory.token_counter import TokenCounter
from app.agent.model_router import ModelRouter, TaskType

logger = logging.getLogger("jarvis.memory.context_compactor")


class CompactionSummary(BaseModel):
    """
    Structured compaction summary complying with Build Plan Section 5.
    Contains all 7 mandatory compaction attributes.
    """
    conversation_summary: str = Field(
        ...,
        description="Concise synthesis of prior conversation turns"
    )
    important_facts: list[str] = Field(
        default_factory=list,
        description="Key factual items and user context extracted from history"
    )
    decisions: list[str] = Field(
        default_factory=list,
        description="Architecture, tech stack, and design decisions finalized"
    )
    open_tasks: list[str] = Field(
        default_factory=list,
        description="Unresolved tasks, action items, or pending requests"
    )
    files_modified: list[str] = Field(
        default_factory=list,
        description="Paths of files modified or referenced during conversation"
    )
    artifacts_created: list[str] = Field(
        default_factory=list,
        description="Artifact names or IDs produced in previous turns"
    )
    tool_state: dict[str, Any] = Field(
        default_factory=dict,
        description="Summary of tool execution states or cached outputs"
    )


def estimate_tokens(text: str) -> int:
    """Fast token estimation heuristic (chars / 4)."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    """Estimate total token count across a list of message dictionaries."""
    total = 0
    for msg in messages:
        total += 4  # overhead per message
        content = msg.get("content", "") or ""
        total += estimate_tokens(content)
        if msg.get("name"):
            total += estimate_tokens(msg["name"])
        if msg.get("tool_calls"):
            total += estimate_tokens(str(msg["tool_calls"]))
    return total


def prune_tool_outputs(
    messages: list[dict[str, Any]],
    char_threshold: int = 200
) -> tuple[list[dict[str, Any]], int]:
    """
    Stage 1 Compaction: Prune bulky tool outputs in older messages.
    """
    pruned_messages = []
    pruned_count = 0
    total_msgs = len(messages)

    for idx, msg in enumerate(messages):
        msg_copy = copy.deepcopy(msg)
        is_recent = (idx >= total_msgs - 2)

        if not is_recent and msg_copy.get("role") == "tool":
            content = msg_copy.get("content", "") or ""
            if len(content) > char_threshold:
                preview = content[:60].replace("\n", " ")
                msg_copy["content"] = f"{preview}... [Tool output pruned: {len(content)} chars]"
                pruned_count += 1

        pruned_messages.append(msg_copy)

    return pruned_messages, pruned_count


async def summarize_older_turns(
    messages: list[dict[str, Any]],
    client: Optional[Any] = None,
    model: Optional[str] = None,
    keep_recent_count: int = 4
) -> list[dict[str, Any]]:
    """
    Stage 2 Compaction: Summarize the older segment of the conversation into concise facts.
    """
    if len(messages) <= keep_recent_count:
        return messages

    split_index = max(1, len(messages) - keep_recent_count)
    older_segment = messages[:split_index]
    recent_segment = messages[split_index:]

    # Format text for summarization
    formatted_lines = []
    for m in older_segment:
        role = m.get("role", "unknown").upper()
        content = m.get("content", "")
        formatted_lines.append(f"{role}: {content}")

    transcript = "\n".join(formatted_lines)
    summary_prompt = (
        "You are an expert memory compactor. Below is an earlier segment of a conversation. "
        "Extract and summarize the key facts, user preferences, decisions, and file names "
        "into concise bullet points:\n\n"
        f"{transcript}\n\n"
        "Bullet Points of Key Facts:"
    )

    try:
        summary_content = ""
        if client and hasattr(client, "chat"):
            target_model = model or "qwen2.5:0.5b"
            summary_res = await client.chat(
                model=target_model,
                messages=[{"role": "user", "content": summary_prompt}]
            )
            if isinstance(summary_res, dict):
                msg = summary_res.get("message", {})
                summary_content = msg.get("content", "") if isinstance(msg, dict) else str(summary_res)
            else:
                summary_content = getattr(summary_res.message, "content", str(summary_res))

        if not summary_content:
            summary_content = f"Earlier conversation history ({len(older_segment)} messages summarized)."

        summary_msg = {
            "role": "system",
            "content": f"[Previous Conversation Context & Summary]\n{summary_content}",
            "is_summary": True
        }

        return [summary_msg] + recent_segment

    except Exception as e:
        logger.error("Failed to generate LLM summary for compaction: %s", e)
        return recent_segment


class ContextCompactor:
    """
    Evaluates conversation token/message limits and performs structured compaction.
    Routes summarization prompts deterministically to FAST_MODEL via ModelRouter.
    Supports both conversation_id-based (Phase 5) and message-list-based (Phase 4) invocations.
    """

    def __init__(
        self,
        memory_store: Optional[MemoryStore] = None,
        model_router: Optional[ModelRouter] = None,
        token_counter: Optional[TokenCounter] = None,
        max_context_tokens: int = 12000,
        max_message_count: int = 20,
        tool_pruning_char_threshold: int = 200,
    ):
        self.memory_store = memory_store or MemoryStore()
        self.model_router = model_router or ModelRouter()
        self.token_counter = token_counter or TokenCounter()
        self.max_context_tokens = max_context_tokens
        self.max_message_count = max_message_count
        self.tool_pruning_char_threshold = tool_pruning_char_threshold

    @staticmethod
    def _no_swap_target(target_model: str, provider: Optional[str], client: Any) -> tuple[str, dict[str, Any]]:
        """
        Compaction is routed to the fast slot, but a slot switch reloads ~5 GB of weights
        (~8 s) -- and the user's next turn switches straight back. Summarising a transcript
        does not need the 4B specifically, it needs *a* model without a thinking preamble.
        So when the fast slot is not the one loaded, the summary runs on main with thinking
        turned off for that one request, exactly as the orchestrator does for quick turns.
        Only real providers understand the ``thinking`` keyword; ad-hoc clients get none.
        """
        if target_model != "fast" or (provider and provider != "llama_cpp"):
            return target_model, {}
        try:
            from app.agent.runtime_process_manager import get_runtime_process_manager
            loaded = get_runtime_process_manager().current_model_kind
        except Exception:
            return target_model, {}
        if loaded == "fast":
            return target_model, {}
        from app.agent.model_provider import ModelProvider
        extra = {"thinking": False} if isinstance(client, ModelProvider) else {}
        return "main", extra

    def should_compact(
        self,
        conversation_id: str,
        message_count_threshold: Optional[int] = None,
        token_budget_threshold: Optional[int] = None,
    ) -> bool:
        """
        Checks if a conversation exceeds message count or token budget thresholds.
        """
        msg_thresh = message_count_threshold or self.max_message_count
        tok_thresh = token_budget_threshold or self.max_context_tokens

        messages = self.memory_store.get_messages(conversation_id)
        if not messages:
            return False

        if len(messages) > msg_thresh:
            logger.info(
                "Conversation '%s' should compact: message count %d exceeds threshold %d",
                conversation_id, len(messages), msg_thresh
            )
            return True

        estimated_tokens = self.token_counter.count_messages(messages)
        if estimated_tokens > tok_thresh:
            logger.info(
                "Conversation '%s' should compact: token count %d exceeds budget %d",
                conversation_id, estimated_tokens, tok_thresh
            )
            return True

        return False

    def _extract_conversation_metadata(
        self,
        messages: list[dict[str, Any]]
    ) -> tuple[list[str], list[str], dict[str, Any]]:
        """
        Extract files modified, artifacts created, and tool state from tool calls.
        """
        files_modified = set()
        artifacts_created = set()
        tool_state = {}

        for msg in messages:
            tool_calls = msg.get("tool_calls")
            if not tool_calls or not isinstance(tool_calls, list):
                continue

            for tc in tool_calls:
                fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                fn_name = fn.get("name", "")
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}

                if fn_name in ("write_file", "patch_file", "edit_file", "create_file"):
                    p = args.get("path") or args.get("file_path") or args.get("target_file")
                    if p:
                        files_modified.add(str(p))
                elif fn_name in ("create_artifact", "save_artifact"):
                    a_name = args.get("name") or args.get("artifact_name") or args.get("id")
                    if a_name:
                        artifacts_created.add(str(a_name))

                if fn_name:
                    tool_state[fn_name] = tool_state.get(fn_name, 0) + 1

        return sorted(list(files_modified)), sorted(list(artifacts_created)), tool_state

    async def _compact_messages_legacy(
        self,
        messages: list[dict[str, Any]],
        client: Optional[Any] = None,
        model: Optional[str] = None,
        threshold_override: Optional[int] = None,
    ) -> tuple[list[dict[str, Any]], Optional[dict[str, Any]]]:
        """
        Progressively compact message history (Phase 4 compatibility).
        """
        threshold = threshold_override or self.max_context_tokens
        tokens_before = self.token_counter.count_messages(messages)

        if tokens_before <= threshold:
            return messages, None

        logger.info(
            "Context compaction triggered: %d tokens exceeds threshold (%d)",
            tokens_before, threshold
        )

        # Stage 1: Prune bulky tool outputs
        pruned_msgs, pruned_count = prune_tool_outputs(
            messages,
            char_threshold=self.tool_pruning_char_threshold
        )
        tokens_stage1 = self.token_counter.count_messages(pruned_msgs)

        if tokens_stage1 <= threshold and pruned_count > 0:
            logger.info("Stage 1 compaction (tool pruning) resolved token load: %d -> %d", tokens_before, tokens_stage1)
            return pruned_msgs, {
                "strategy": "tool_pruning",
                "tokens_before": tokens_before,
                "tokens_after": tokens_stage1,
                "details": f"Pruned {pruned_count} bulky tool outputs"
            }

        # Stage 2: LLM Summarization of older turns
        summarized_msgs = await summarize_older_turns(
            pruned_msgs,
            client=client,
            model=model
        )
        tokens_stage2 = self.token_counter.count_messages(summarized_msgs)

        logger.info("Stage 2 compaction (summarization) completed: %d -> %d", tokens_before, tokens_stage2)
        return summarized_msgs, {
            "strategy": "summarization",
            "tokens_before": tokens_before,
            "tokens_after": tokens_stage2,
            "details": f"Summarized earlier conversation history ({tokens_before} -> {tokens_stage2} tokens)"
        }

    async def compact(
        self,
        conversation_id: Optional[Union[str, list[dict[str, Any]]]] = None,
        client: Optional[Any] = None,
        model_provider: Optional[Any] = None,
        keep_recent_count: int = 4,
        # Phase 4 kwargs
        messages: Optional[list[dict[str, Any]]] = None,
        model: Optional[str] = None,
        threshold_override: Optional[int] = None,
    ) -> Union[CompactionSummary, tuple[list[dict[str, Any]], Optional[dict[str, Any]]]]:
        """
        Multi-mode compaction entrypoint:
        1. If passed messages list (or messages=...), executes Phase 4 progressive compaction returning (msgs, info).
        2. If passed conversation_id string, executes Phase 5 structured compaction returning CompactionSummary.
        """
        # Check if called in Phase 4 message-list mode
        if isinstance(conversation_id, list):
            return await self._compact_messages_legacy(
                messages=conversation_id,
                client=client or model_provider,
                model=model,
                threshold_override=threshold_override
            )
        if messages is not None:
            return await self._compact_messages_legacy(
                messages=messages,
                client=client or model_provider,
                model=model,
                threshold_override=threshold_override
            )

        # Phase 5: Structured compaction for conversation_id
        session_id = str(conversation_id or "default")
        msgs = self.memory_store.get_messages(session_id)
        tokens_before = self.token_counter.count_messages(msgs)

        # 1. Extract metadata from message tool calls
        files_mod, artifacts_cr, tool_st = self._extract_conversation_metadata(msgs)

        # 2. Stage 1: Prune bulky tool outputs
        pruned_messages, pruned_count = prune_tool_outputs(
            msgs,
            char_threshold=self.tool_pruning_char_threshold
        )

        # 3. Separate older segment from recent turns
        if len(pruned_messages) > keep_recent_count:
            split_idx = max(1, len(pruned_messages) - keep_recent_count)
            older_segment = pruned_messages[:split_idx]
            recent_segment = pruned_messages[split_idx:]
        else:
            older_segment = pruned_messages
            recent_segment = []

        transcript_lines = []
        for m in older_segment:
            role = m.get("role", "user").upper()
            content = m.get("content", "")
            transcript_lines.append(f"{role}: {content}")
        transcript = "\n".join(transcript_lines)

        # 4. Route deterministically to FAST_MODEL
        routing = self.model_router.route_task(
            task_type=TaskType.COMPACTION,
            message="Summarize prior conversation turns for compaction"
        )
        target_model = model or routing.model
        target_model, chat_extra = self._no_swap_target(target_model, routing.provider, model_provider or client)
        logger.info(
            "Compaction prompt routed to %s (%s) for session '%s'",
            target_model, routing.provider, session_id
        )

        prompt = (
            "You are an expert conversation memory compactor. "
            "Analyze the conversation transcript below and provide a structured JSON summary.\n\n"
            f"TRANSCRIPT:\n{transcript}\n\n"
            "Return valid JSON ONLY in this format:\n"
            "{\n"
            '  "conversation_summary": "Concise summary of conversation",\n'
            '  "important_facts": ["fact 1", "fact 2"],\n'
            '  "decisions": ["decision 1"],\n'
            '  "open_tasks": ["task 1"],\n'
            '  "files_modified": [],\n'
            '  "artifacts_created": [],\n'
            '  "tool_state": {}\n'
            "}"
        )

        summary_data: dict[str, Any] = {}
        raw_response = ""

        # 5. Invoke LLM via model_provider or client
        active_client = model_provider or client
        try:
            if active_client and hasattr(active_client, "chat"):
                res = await active_client.chat(
                    messages=[{"role": "user", "content": prompt}],
                    model=target_model,
                    temperature=0.2,
                    **chat_extra,
                )
                if isinstance(res, dict):
                    msg_obj = res.get("message", {})
                    raw_response = msg_obj.get("content", "") if isinstance(msg_obj, dict) else str(res)
                else:
                    raw_response = getattr(res, "content", "") or getattr(getattr(res, "message", None), "content", str(res))

            # Parse JSON from response
            if raw_response:
                json_match = re.search(r"\{.*\}", raw_response, re.DOTALL)
                if json_match:
                    try:
                        summary_data = json.loads(json_match.group(0))
                    except Exception:
                        pass

        except Exception as e:
            logger.warning("LLM call during compaction failed (%s). Using fallback summary.", e)

        # Fallback summary if LLM or JSON parsing failed
        if not summary_data or not summary_data.get("conversation_summary"):
            fallback_summary = (
                f"Conversation spanning {len(msgs)} messages. "
                f"Pruned {pruned_count} bulky tool outputs."
            )
            summary_data = {
                "conversation_summary": raw_response.strip() if raw_response else fallback_summary,
                "important_facts": summary_data.get("important_facts", []),
                "decisions": summary_data.get("decisions", []),
                "open_tasks": summary_data.get("open_tasks", []),
                "files_modified": summary_data.get("files_modified") or files_mod,
                "artifacts_created": summary_data.get("artifacts_created") or artifacts_cr,
                "tool_state": summary_data.get("tool_state") or tool_st,
            }

        # Merge extracted metadata if not already filled by LLM
        if not summary_data.get("files_modified") and files_mod:
            summary_data["files_modified"] = files_mod
        if not summary_data.get("artifacts_created") and artifacts_cr:
            summary_data["artifacts_created"] = artifacts_cr
        if not summary_data.get("tool_state") and tool_st:
            summary_data["tool_state"] = tool_st

        compaction_summary = CompactionSummary(**summary_data)

        # 6. Format summary message and replace older history in store
        summary_msg = {
            "role": "system",
            "content": f"[Prior Conversation Summary]\n{compaction_summary.conversation_summary}",
            "is_summary": True
        }
        compacted_messages = [summary_msg] + recent_segment
        self.memory_store.replace_messages(session_id, compacted_messages)

        tokens_after = self.token_counter.count_messages(compacted_messages)

        # 7. Record compaction event in compaction_events table
        details_payload = json.dumps({
            "summary": compaction_summary.conversation_summary,
            "compaction_summary": compaction_summary.model_dump(),
            "pruned_tool_outputs": pruned_count
        })

        self.memory_store.record_compaction(
            session_id=session_id,
            strategy="structured_summarization",
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            details=details_payload
        )

        logger.info(
            "Compacted conversation '%s': %d -> %d tokens (Pruned %d tools)",
            session_id, tokens_before, tokens_after, pruned_count
        )

        return compaction_summary


# Standalone function helpers
def should_compact(
    conversation_id: str,
    message_count_threshold: int = 20,
    token_budget_threshold: int = 12000,
    memory_store: Optional[MemoryStore] = None,
    token_counter: Optional[TokenCounter] = None,
) -> bool:
    compactor = ContextCompactor(
        memory_store=memory_store,
        token_counter=token_counter,
        max_message_count=message_count_threshold,
        max_context_tokens=token_budget_threshold,
    )
    return compactor.should_compact(conversation_id)


async def compact(
    conversation_id: Optional[Union[str, list[dict[str, Any]]]] = None,
    client: Optional[Any] = None,
    model_provider: Optional[Any] = None,
    memory_store: Optional[MemoryStore] = None,
    model_router: Optional[ModelRouter] = None,
    token_counter: Optional[TokenCounter] = None,
    # Phase 4 kwargs
    messages: Optional[list[dict[str, Any]]] = None,
    model: Optional[str] = None,
    threshold_override: Optional[int] = None,
) -> Union[CompactionSummary, tuple[list[dict[str, Any]], Optional[dict[str, Any]]]]:
    compactor = ContextCompactor(
        memory_store=memory_store,
        model_router=model_router,
        token_counter=token_counter,
    )
    return await compactor.compact(
        conversation_id=conversation_id,
        client=client,
        model_provider=model_provider,
        messages=messages,
        model=model,
        threshold_override=threshold_override
    )
