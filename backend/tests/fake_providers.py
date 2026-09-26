"""
Test doubles for driving AgentOrchestrator with scripted model replies.

``ClientBackedProvider`` is the old orchestrator-internal ``_LegacyClientAdapter`` moved here when
the Ollama / LM Studio fallbacks were deleted: it wraps any fake client with a ``chat(model=,
messages=, tools=)`` method (Ollama-shaped objects or plain dicts) and, optionally,
``chat_stream(...)`` yielding ``{"type": "token"|"done"}`` events. Tests that go through the HTTP
endpoints install one with ``use_model_client(fake)``, which replaces the provider the
orchestrator gets when ``app.main`` constructs it.
"""
from typing import Any, AsyncIterator
from unittest.mock import patch

from app.agent.model_provider import ModelProvider


class ClientBackedProvider(ModelProvider):
    def __init__(self, client: Any, name_str: str = "llama_cpp"):
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

    async def chat(self, messages, model=None, tools=None, temperature=None, profile="general", timeout=None, thinking=None, tool_choice=None) -> dict[str, Any]:
        res = await self.client.chat(model=model, messages=messages, tools=tools)
        if isinstance(res, dict):
            return res
        msg = getattr(res, "message", None)
        if msg is not None:
            return {
                "message": {
                    "role": "assistant",
                    "content": getattr(msg, "content", "") or "",
                    "tool_calls": getattr(msg, "tool_calls", None),
                },
                "raw": res,
            }
        return {"message": {"role": "assistant", "content": str(res), "tool_calls": None}, "raw": res}

    async def stream_chat(self, messages, model=None, tools=None, temperature=None, profile="general", timeout=None, thinking=None) -> AsyncIterator[dict[str, Any]]:
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


def use_model_client(client: Any):
    """Patch context: every AgentOrchestrator built without an explicit provider uses ``client``."""
    return patch("app.agent.orchestrator.get_model_provider", return_value=ClientBackedProvider(client))
