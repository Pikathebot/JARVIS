import contextlib
import json
import logging
import re
from typing import Any, AsyncIterator, Optional
import httpx
from app.config import settings
from app.agent.model_provider import ModelProvider
from app.agent.tool_schema import convert_tool_to_openai_schema
from app.agent.runtime_process_manager import RuntimeProcessManager, get_runtime_process_manager

logger = logging.getLogger("jarvis.agent.llamacpp")

# Regex to strip <think>...</think> reasoning blocks
THINK_TAG_REGEX = re.compile(r"<think>[\s\S]*?</think>", re.DOTALL)

_OPEN_TAG = "<think>"
_CLOSE_TAG = "</think>"
# Longest tag we might need to detect split across two deltas, minus one -- that's how much of a
# trailing fragment must be held back rather than emitted, in case the next chunk completes a tag.
_MAX_HOLDBACK = max(len(_OPEN_TAG), len(_CLOSE_TAG)) - 1


class ThinkTagStreamScanner:
    """
    Classifies streamed content-delta text as reasoning vs answer, for models (like llama-server's
    default Qwen3.5 template with no --reasoning-format set) that emit reasoning as literal
    <think>...</think> tags inline in the content stream rather than a separate API field.

    A tag can straddle two SSE deltas (e.g. one chunk ends "...<thi", the next starts "nk>...").
    feed() only emits text once it is certain no held-back suffix is a tag fragment; flush() drains
    whatever remains at stream end (as answer/reasoning text depending on last known state, since an
    unterminated tag at end-of-stream can't be anything else).
    """

    def __init__(self) -> None:
        self._in_think = False
        self._buf = ""

    def feed(self, chunk: str) -> list[tuple[str, str]]:
        self._buf += chunk
        out: list[tuple[str, str]] = []

        while True:
            open_idx = self._buf.find(_OPEN_TAG)
            close_idx = self._buf.find(_CLOSE_TAG)
            tag_idx = close_idx if self._in_think else open_idx
            tag = _CLOSE_TAG if self._in_think else _OPEN_TAG

            if tag_idx != -1:
                before = self._buf[:tag_idx]
                if before:
                    out.append(("reasoning" if self._in_think else "answer", before))
                self._in_think = not self._in_think
                self._buf = self._buf[tag_idx + len(tag):]
                continue

            # No complete tag found. Emit everything except a trailing fragment that could still
            # turn into the start of a tag once more of the stream arrives.
            safe_len = max(0, len(self._buf) - _MAX_HOLDBACK)
            if safe_len > 0:
                emit, self._buf = self._buf[:safe_len], self._buf[safe_len:]
                out.append(("reasoning" if self._in_think else "answer", emit))
            break

        return out

    def flush(self) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        if self._buf:
            out.append(("reasoning" if self._in_think else "answer", self._buf))
            self._buf = ""
        return out


def strip_thinking_tags(text: str) -> tuple[str, str]:
    """
    Strips <think>...</think> reasoning blocks from text.
    Returns (cleaned_text, extracted_reasoning).
    """
    if not text or not isinstance(text, str):
        return text or "", ""
    reasoning_blocks = THINK_TAG_REGEX.findall(text)
    extracted_reasoning = "\n".join([b.replace("<think>", "").replace("</think>", "").strip() for b in reasoning_blocks])
    cleaned = THINK_TAG_REGEX.sub("", text).strip()
    return cleaned, extracted_reasoning


def _dump_payload(payload: dict[str, Any], stream: bool) -> None:
    """Write the request to settings.llama_payload_dump_dir (see config); never raises."""
    target = getattr(settings, "llama_payload_dump_dir", None)
    if not target:
        return
    try:
        import copy
        import time
        from pathlib import Path

        out_dir = Path(target)
        out_dir.mkdir(parents=True, exist_ok=True)
        clone = copy.deepcopy(payload)
        for msg in clone.get("messages", []):
            content = msg.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and "image_url" in part:
                        part["image_url"] = {"url": "<elided>"}
        name = f"{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}-{'stream' if stream else 'chat'}.json"
        (out_dir / name).write_text(json.dumps(clone, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:  # pragma: no cover - debug aid only
        logger.debug("Payload dump skipped: %s", exc)


def _thinking_override(thinking: Optional[bool]) -> dict[str, Any]:
    """
    Per-request reasoning switch. llama-server applies ``chat_template_kwargs`` from the request
    body on top of the launch-time ``--chat-template-kwargs``, so one loaded model can answer a
    quick question directly and reason through a tool turn without a reload: measured on the
    9B, "17*23?" took 19.3 s with thinking (434 tokens) and 0.4 s without (4 tokens), same
    answer. None means "as launched".
    """
    if thinking is None:
        return {}
    return {"chat_template_kwargs": {"enable_thinking": bool(thinking)}}


def _sanitize_messages_for_jinja(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Sanitizes conversation messages for Qwen3.5 Jinja chat template.
    Consolidates all 'system' role messages into a single system message at index 0
    to strictly satisfy Jinja template assertion: 'System message must be at the beginning.'
    """
    if not messages:
        return []

    system_parts = []
    non_system_messages = []

    for m in messages:
        if m.get("role") == "system":
            c = m.get("content")
            if c:
                system_parts.append(str(c).strip())
        else:
            non_system_messages.append(m)

    sanitized = []
    if system_parts:
        merged_system = "\n\n".join(system_parts)
        sanitized.append({"role": "system", "content": merged_system})

    sanitized.extend(non_system_messages)
    return sanitized


class LlamaCppProvider(ModelProvider):
    """
    Primary local model provider communicating with llama-server.exe
    via its OpenAI-compatible /v1 endpoints.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout: float = 90.0,
        process_manager: Optional[RuntimeProcessManager] = None
    ):
        self.host = settings.llamacpp_host
        self.port = settings.llamacpp_port
        self.base_url = (base_url or f"http://{self.host}:{self.port}").rstrip("/")
        self.timeout = timeout
        self.process_manager = process_manager or get_runtime_process_manager()

    @property
    def name(self) -> str:
        return "llama_cpp"

    def _get_headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
        }

    def _get_sampling_parameters(self, profile: str = "general", custom_temp: Optional[float] = None) -> dict[str, Any]:
        """
        Calibrated sampling parameters for Qwen3.5 per Unsloth guidance.
        - general: temp 0.7, top_p 0.8, top_k 20, min_p 0.0, presence_penalty 1.5
        - coding:  temp 0.6, top_p 0.95, top_k 20, min_p 0.0, presence_penalty 0.0
        """
        prof = (profile or "general").strip().lower()
        if prof in ("coding", "code", "tool", "tools"):
            params = {
                "temperature": 0.6 if custom_temp is None else custom_temp,
                "top_p": 0.95,
                "top_k": 20,
                "min_p": 0.0,
                "presence_penalty": 0.0,
            }
        else:
            params = {
                "temperature": 0.7 if custom_temp is None else custom_temp,
                "top_p": 0.8,
                "top_k": 20,
                "min_p": 0.0,
                "presence_penalty": 1.5,
            }
        return params

    async def health_check(self) -> bool:
        """Probe GET /v1/models endpoint."""
        url = f"{self.base_url}/v1/models"
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(url, headers=self._get_headers())
                return resp.status_code == 200
        except Exception:
            return False

    async def model_info(self) -> dict[str, Any]:
        """Retrieve model metadata from llama-server."""
        models = await self.list_models()
        return {
            "provider": self.name,
            "base_url": self.base_url,
            "running_model_kind": self.process_manager.current_model_kind,
            "available_models": models,
        }

    async def list_models(self) -> list[str]:
        """List active/available models from llama-server."""
        url = f"{self.base_url}/v1/models"
        models: list[str] = []
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(url, headers=self._get_headers())
                if resp.status_code == 200:
                    data = resp.json()
                    for item in data.get("data", []):
                        m_id = item.get("id") or item.get("name")
                        if m_id:
                            models.append(m_id)
        except Exception as e:
            logger.warning("Failed to list models from llama-server (%s): %s", self.base_url, e)
        return models

    async def unload_model(self, model: Optional[str] = None) -> bool:
        """
        Evict active model and release GPU VRAM by terminating the server process.
        Never raises exceptions into callers.
        """
        try:
            return await self.process_manager.stop()
        except Exception as e:
            logger.warning("Error delegating unload_model to RuntimeProcessManager: %s", e)
            return False

    async def _ensure_server_ready(self, model_kind: str = "main") -> None:
        """Verify server readiness and ensure the requested model is loaded."""
        await self.process_manager.ensure_running(model_kind=model_kind)

    def _normalize_tool_calls(self, raw_tool_calls: Optional[list[dict[str, Any]]]) -> Optional[list[dict[str, Any]]]:
        """Normalize tool calls list to standard format with safely parsed arguments."""
        if not raw_tool_calls or not isinstance(raw_tool_calls, list):
            return None

        normalized = []
        for tc in raw_tool_calls:
            if not isinstance(tc, dict):
                continue
            fn_obj = tc.get("function", {})
            fn_name = fn_obj.get("name", "")
            raw_args = fn_obj.get("arguments", {})
            
            if isinstance(raw_args, str):
                try:
                    parsed_args = json.loads(raw_args)
                except Exception:
                    # Fallback to empty dict with debug metadata without crashing
                    parsed_args = {}
            elif isinstance(raw_args, dict):
                parsed_args = raw_args
            else:
                parsed_args = {}

            call_id = tc.get("id") or f"call_{len(normalized)}"
            normalized.append({
                "id": str(call_id),
                "type": "function",
                "function": {
                    "name": fn_name,
                    "arguments": parsed_args
                }
            })
        return normalized if normalized else None

    @staticmethod
    def _offload_images_if_overflow(payload: dict[str, Any], status_code: int, body: str) -> bool:
        """
        If a request with image parts was rejected for not fitting the context, rewrite the
        payload's messages with the images replaced by placeholders so the caller can retry once.
        Returns True when the payload was changed.
        """
        from app.agent.image_offload import has_image_parts, looks_like_context_overflow, offload_images

        messages = payload.get("messages") or []
        if not has_image_parts(messages) or not looks_like_context_overflow(status_code, body):
            return False
        stripped, count = offload_images(messages)
        payload["messages"] = stripped
        logger.warning(
            "llama-server rejected the request (HTTP %s); retrying with %d image(s) replaced by placeholders.",
            status_code, count,
        )
        return True

    async def chat(
        self,
        messages: list[dict[str, Any]],
        model: Optional[str] = None,
        tools: Optional[list[Any]] = None,
        temperature: Optional[float] = None,
        profile: str = "general",
        timeout: Optional[float] = None,
        thinking: Optional[bool] = None,
    ) -> dict[str, Any]:
        """
        Send a non-streaming chat completion request to llama-server.
        Retries once if server is offline by invoking ensure_running.
        """
        target_model_kind = model or "main"
        req_timeout = timeout or self.timeout
        url = f"{self.base_url}/v1/chat/completions"

        sampling = self._get_sampling_parameters(profile=profile, custom_temp=temperature)

        payload: dict[str, Any] = {
            "model": target_model_kind,
            "messages": _sanitize_messages_for_jinja(messages),
            "stream": False,
            **sampling,
            **_thinking_override(thinking),
        }

        if tools:
            formatted_tools = []
            for t in tools:
                schema = convert_tool_to_openai_schema(t)
                if schema:
                    formatted_tools.append(schema)
            if formatted_tools:
                payload["tools"] = formatted_tools
                payload["tool_choice"] = "auto"

        client_timeout = httpx.Timeout(connect=30.0, read=300.0, write=60.0, pool=None)
        if isinstance(req_timeout, (int, float)):
            client_timeout = httpx.Timeout(connect=30.0, read=float(req_timeout), write=60.0, pool=None)

        _dump_payload(payload, stream=False)
        # Attempt call with retry on connection failure
        for attempt in (1, 2):
            try:
                await self._ensure_server_ready(target_model_kind)
                async with httpx.AsyncClient(timeout=client_timeout) as client:
                    resp = await client.post(url, json=payload, headers=self._get_headers())
                    if resp.status_code != 200 and self._offload_images_if_overflow(payload, resp.status_code, resp.text):
                        resp = await client.post(url, json=payload, headers=self._get_headers())
                    if resp.status_code != 200:
                        raise RuntimeError(f"llama-server returned HTTP {resp.status_code}: {resp.text}")
                    data = resp.json()
                    break
            except Exception as e:
                if attempt == 1:
                    logger.warning("llama-server request failed on attempt 1 (%s). Ensuring server is running and retrying...", e)
                    await self.process_manager.ensure_running(target_model_kind)
                else:
                    logger.error("llama-server request failed on retry: %s", e)
                    raise RuntimeError(f"llama-server chat failed: {e}") from e

        choices = data.get("choices", [])
        if not choices:
            return {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": None
                },
                "raw": data
            }

        msg = choices[0].get("message", {})
        raw_content = msg.get("content") or ""
        raw_reasoning = msg.get("reasoning_content") or ""
        cleaned_content, extracted_reasoning = strip_thinking_tags(raw_content)
        final_reasoning = (raw_reasoning + "\n" + extracted_reasoning).strip()

        tool_calls = msg.get("tool_calls")
        normalized_calls = self._normalize_tool_calls(tool_calls)

        return {
            "message": {
                "role": "assistant",
                "content": cleaned_content,
                "reasoning_content": final_reasoning,
                "tool_calls": normalized_calls
            },
            "raw": data
        }

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        model: Optional[str] = None,
        tools: Optional[list[Any]] = None,
        temperature: Optional[float] = None,
        profile: str = "general",
        timeout: Optional[float] = None,
        thinking: Optional[bool] = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """
        Stream chat tokens and tool calls from llama-server.
        Yields:
        - {"event": "text_delta", "content": str}
        - {"event": "reasoning_delta", "content": str}
        - {"event": "tool_call", "tool_call": dict}
        - {"event": "done", "raw": dict}
        - {"event": "error", "message": str}
        """
        target_model_kind = model or "main"
        req_timeout = timeout or self.timeout
        url = f"{self.base_url}/v1/chat/completions"

        sampling = self._get_sampling_parameters(profile=profile, custom_temp=temperature)

        payload: dict[str, Any] = {
            "model": target_model_kind,
            "messages": _sanitize_messages_for_jinja(messages),
            "stream": True,
            **sampling,
            **_thinking_override(thinking),
        }

        if tools:
            formatted_tools = []
            for t in tools:
                schema = convert_tool_to_openai_schema(t)
                if schema:
                    formatted_tools.append(schema)
            if formatted_tools:
                payload["tools"] = formatted_tools
                payload["tool_choice"] = "auto"

        _dump_payload(payload, stream=True)
        try:
            await self._ensure_server_ready(target_model_kind)
        except Exception as e:
            logger.error("Failed to ensure llama-server running for stream: %s", e)
            yield {"event": "error", "message": str(e)}
            return

        accumulated_content: list[str] = []
        accumulated_reasoning: list[str] = []
        tool_calls_map: dict[int, dict[str, Any]] = {}
        think_scanner = ThinkTagStreamScanner()

        client_timeout = httpx.Timeout(connect=30.0, read=300.0, write=60.0, pool=None)
        if isinstance(req_timeout, (int, float)):
            client_timeout = httpx.Timeout(connect=30.0, read=max(300.0, float(req_timeout)), write=60.0, pool=None)

        try:
            async with httpx.AsyncClient(timeout=client_timeout) as client:
                async with contextlib.AsyncExitStack() as stack:
                    response = await stack.enter_async_context(
                        client.stream("POST", url, json=payload, headers=self._get_headers())
                    )
                    if response.status_code != 200:
                        err_body = (await response.aread()).decode("utf-8", errors="replace")
                        if self._offload_images_if_overflow(payload, response.status_code, err_body):
                            await stack.aclose()
                            response = await stack.enter_async_context(
                                client.stream("POST", url, json=payload, headers=self._get_headers())
                            )
                            if response.status_code != 200:
                                err_body = (await response.aread()).decode("utf-8", errors="replace")
                    if response.status_code != 200:
                        err_msg = f"llama-server streaming HTTP {response.status_code}: {err_body}"
                        yield {"event": "error", "message": err_msg}
                        return

                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        if line.startswith("data: "):
                            raw_data = line[6:].strip()
                            if raw_data == "[DONE]":
                                break
                            try:
                                chunk = json.loads(raw_data)
                            except Exception:
                                continue

                            choices = chunk.get("choices", [])
                            if not choices:
                                continue
                            delta = choices[0].get("delta", {})

                            content_delta = delta.get("content")
                            if content_delta:
                                for kind, text in think_scanner.feed(content_delta):
                                    if kind == "reasoning":
                                        accumulated_reasoning.append(text)
                                        yield {"event": "reasoning_delta", "content": text}
                                    else:
                                        accumulated_content.append(text)
                                        yield {"event": "text_delta", "content": text}

                            reasoning_delta = delta.get("reasoning_content")
                            if reasoning_delta:
                                accumulated_reasoning.append(reasoning_delta)
                                yield {"event": "reasoning_delta", "content": reasoning_delta}

                            tc_deltas = delta.get("tool_calls")
                            if tc_deltas and isinstance(tc_deltas, list):
                                for tc in tc_deltas:
                                    idx = tc.get("index", 0)
                                    if idx not in tool_calls_map:
                                        tool_calls_map[idx] = {
                                            "id": tc.get("id") or f"call_{idx}",
                                            "name": "",
                                            "args_chunks": []
                                        }
                                    if tc.get("id"):
                                        tool_calls_map[idx]["id"] = tc["id"]
                                    fn = tc.get("function", {})
                                    if fn.get("name"):
                                        tool_calls_map[idx]["name"] = fn["name"]
                                    if fn.get("arguments"):
                                        tool_calls_map[idx]["args_chunks"].append(fn["arguments"])
                                        yield {
                                            "event": "tool_draft",
                                            "tool": tool_calls_map[idx]["name"] or "action",
                                            "args_delta": fn["arguments"]
                                        }

        except Exception as e:
            logger.error("llama-server streaming exception (%s): %s", type(e).__name__, e)
            yield {"event": "error", "message": f"{type(e).__name__}: {e}"}
            return

        for kind, text in think_scanner.flush():
            if kind == "reasoning":
                accumulated_reasoning.append(text)
                yield {"event": "reasoning_delta", "content": text}
            else:
                accumulated_content.append(text)
                yield {"event": "text_delta", "content": text}

        # Assemble complete tool calls and emit events
        final_tool_calls = []
        if tool_calls_map:
            for idx in sorted(tool_calls_map.keys()):
                tc_data = tool_calls_map[idx]
                raw_args_str = "".join(tc_data["args_chunks"])
                try:
                    parsed_args = json.loads(raw_args_str) if raw_args_str.strip() else {}
                except Exception:
                    parsed_args = {}

                complete_call = {
                    "id": tc_data["id"],
                    "type": "function",
                    "function": {
                        "name": tc_data["name"],
                        "arguments": parsed_args
                    }
                }
                final_tool_calls.append(complete_call)
                yield {"event": "tool_call", "tool_call": complete_call}

        full_content = "".join(accumulated_content)
        full_reasoning = "".join(accumulated_reasoning)

        # A turn that is all reasoning and no answer stays that way: the orchestrator decides
        # whether to nudge the model on (it usually stopped right before a tool call) rather
        # than having the thinking shown as if it were the reply.

        yield {
            "event": "done",
            "raw": {
                "content": full_content,
                "reasoning": full_reasoning,
                "tool_calls": final_tool_calls if final_tool_calls else None
            }
        }
