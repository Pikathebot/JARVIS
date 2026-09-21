import json
import logging
from typing import Any, AsyncIterator, Optional
import httpx
from app.config import settings
from app.agent.model_provider import ModelProvider
from app.agent.tool_schema import convert_tool_to_openai_schema

logger = logging.getLogger("jarvis.agent.ollama")


class OllamaProvider(ModelProvider):
    """
    Fallback model provider communicating with Ollama's native REST API.
    Refuses Qwen3.5 models due to Ollama mmproj compatibility issues.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        default_model: Optional[str] = None,
        timeout: float = 90.0
    ):
        self.base_url = (base_url or getattr(settings, "ollama_base_url", settings.ollama_host)).rstrip("/")
        self.default_model = default_model or settings.ollama_main_model
        self.timeout = timeout

    @property
    def name(self) -> str:
        return "ollama"

    def _validate_model_allowed(self, model_name: str) -> None:
        """Enforce strict ban on running Qwen3.5 models through Ollama."""
        m_lower = (model_name or "").strip().lower()
        if m_lower.startswith("qwen3.5") or "qwen3.5" in m_lower or "qwen-3.5" in m_lower:
            raise ValueError(
                f"Model '{model_name}' is not allowed on Ollama runtime due to known mmproj/GGUF compatibility issues. "
                f"Qwen3.5 models must be executed via the primary llama.cpp runtime."
            )

    async def health_check(self) -> bool:
        """Probe GET /api/tags endpoint."""
        url = f"{self.base_url}/api/tags"
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(url)
                return resp.status_code == 200
        except Exception:
            return False

    async def model_info(self) -> dict[str, Any]:
        """Retrieve model info from Ollama."""
        models = await self.list_models()
        return {
            "provider": self.name,
            "base_url": self.base_url,
            "default_model": self.default_model,
            "available_models": models,
        }

    async def list_models(self) -> list[str]:
        """List models available in Ollama."""
        url = f"{self.base_url}/api/tags"
        models: list[str] = []
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    for item in data.get("models", []):
                        name = item.get("name") or item.get("model")
                        if name:
                            models.append(name)
        except Exception as e:
            logger.warning("Failed to list models from Ollama (%s): %s", self.base_url, e)
        return models

    async def unload_model(self, model: Optional[str] = None) -> bool:
        """
        Unload model from VRAM via Ollama /api/generate with keep_alive: 0.
        """
        target_model = model or self.default_model
        url = f"{self.base_url}/api/generate"
        payload = {
            "model": target_model,
            "prompt": "",
            "stream": False,
            "keep_alive": 0
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=payload)
                return resp.status_code == 200
        except Exception as e:
            logger.warning("Error unloading model '%s' from Ollama: %s", target_model, e)
            return False

    def _normalize_tool_calls(self, raw_tool_calls: Optional[list[Any]]) -> Optional[list[dict[str, Any]]]:
        if not raw_tool_calls or not isinstance(raw_tool_calls, list):
            return None

        normalized = []
        for tc in raw_tool_calls:
            if isinstance(tc, dict):
                fn_obj = tc.get("function", {})
                fn_name = fn_obj.get("name", "")
                fn_args = fn_obj.get("arguments", {})
            else:
                fn_obj = getattr(tc, "function", None)
                fn_name = getattr(fn_obj, "name", "")
                fn_args = getattr(fn_obj, "arguments", {})

            if isinstance(fn_args, str):
                try:
                    fn_args = json.loads(fn_args)
                except Exception:
                    fn_args = {}
            elif not isinstance(fn_args, dict):
                fn_args = {}

            call_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
            call_id = call_id or f"call_{len(normalized)}"

            normalized.append({
                "id": str(call_id),
                "type": "function",
                "function": {
                    "name": fn_name,
                    "arguments": fn_args
                }
            })
        return normalized if normalized else None

    async def chat(
        self,
        messages: list[dict[str, Any]],
        model: Optional[str] = None,
        tools: Optional[list[Any]] = None,
        temperature: float = 0.7,
        profile: str = "general",
        timeout: Optional[float] = None,
        thinking: Optional[bool] = None,
    ) -> dict[str, Any]:
        """Send chat request to Ollama native /api/chat endpoint."""
        target_model = model or self.default_model
        self._validate_model_allowed(target_model)

        req_timeout = timeout or self.timeout
        url = f"{self.base_url}/api/chat"

        payload: dict[str, Any] = {
            "model": target_model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": temperature
            }
        }

        if tools:
            formatted_tools = []
            for t in tools:
                schema = convert_tool_to_openai_schema(t)
                if schema:
                    formatted_tools.append(schema)
            if formatted_tools:
                payload["tools"] = formatted_tools

        async with httpx.AsyncClient(timeout=req_timeout) as client:
            try:
                resp = await client.post(url, json=payload)
            except Exception as e:
                logger.error("Ollama connection error: %s", e)
                raise RuntimeError(f"Ollama connection error: {e}") from e

        if resp.status_code != 200:
            raise RuntimeError(f"Ollama returned HTTP {resp.status_code}: {resp.text}")

        data = resp.json()
        msg_obj = data.get("message", {})
        content = msg_obj.get("content", "") or ""
        tool_calls = msg_obj.get("tool_calls")
        normalized_calls = self._normalize_tool_calls(tool_calls)

        return {
            "message": {
                "role": "assistant",
                "content": content,
                "tool_calls": normalized_calls
            },
            "raw": data
        }

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        model: Optional[str] = None,
        tools: Optional[list[Any]] = None,
        temperature: float = 0.7,
        profile: str = "general",
        timeout: Optional[float] = None,
        thinking: Optional[bool] = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream chat tokens and tool calls from Ollama /api/chat."""
        target_model = model or self.default_model
        try:
            self._validate_model_allowed(target_model)
        except ValueError as e:
            yield {"event": "error", "message": str(e)}
            return

        req_timeout = timeout or self.timeout
        url = f"{self.base_url}/api/chat"

        payload: dict[str, Any] = {
            "model": target_model,
            "messages": messages,
            "stream": True,
            "options": {
                "temperature": temperature
            }
        }

        if tools:
            formatted_tools = []
            for t in tools:
                schema = convert_tool_to_openai_schema(t)
                if schema:
                    formatted_tools.append(schema)
            if formatted_tools:
                payload["tools"] = formatted_tools

        accumulated_content = []
        collected_tool_calls = []

        try:
            async with httpx.AsyncClient(timeout=req_timeout) as client:
                async with client.stream("POST", url, json=payload) as response:
                    if response.status_code != 200:
                        err_body = await response.aread()
                        yield {"event": "error", "message": f"Ollama streaming HTTP {response.status_code}: {err_body.decode('utf-8', errors='replace')}"}
                        return

                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        try:
                            chunk = json.loads(line)
                        except Exception:
                            continue

                        msg = chunk.get("message", {})
                        content_chunk = msg.get("content", "")
                        if content_chunk:
                            accumulated_content.append(content_chunk)
                            yield {"event": "text_delta", "content": content_chunk}

                        tc_chunk = msg.get("tool_calls")
                        if tc_chunk:
                            norm = self._normalize_tool_calls(tc_chunk)
                            if norm:
                                for c in norm:
                                    collected_tool_calls.append(c)
                                    yield {"event": "tool_call", "tool_call": c}

                        if chunk.get("done"):
                            break
        except Exception as e:
            logger.error("Ollama streaming error: %s", e)
            yield {"event": "error", "message": str(e)}
            return

        yield {
            "event": "done",
            "raw": {
                "content": "".join(accumulated_content),
                "tool_calls": collected_tool_calls if collected_tool_calls else None
            }
        }
