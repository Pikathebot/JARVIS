from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Optional


class ModelProvider(ABC):
    """
    Abstract interface for local and remote LLM execution runtimes.
    Enforces normalized output formats across all providers.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider name identifier (e.g. 'llama_cpp', 'ollama')."""
        pass

    @abstractmethod
    async def health_check(self) -> bool:
        """Check if the backend runtime server is online, reachable, and ready."""
        pass

    @abstractmethod
    async def model_info(self) -> dict[str, Any]:
        """Fetch metadata/info for the currently active or configured model."""
        pass

    @abstractmethod
    async def list_models(self) -> list[str]:
        """List available/active model IDs or tags from the runtime."""
        pass

    @abstractmethod
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
        """
        Send a non-streaming chat completion request.

        ``thinking`` overrides the model's launch-time reasoning setting for this one request
        (None = leave it as launched). Providers without a reasoning switch ignore it.
        Returns normalized dictionary:
        {
            "message": {
                "role": "assistant",
                "content": str,
                "tool_calls": list[dict] | None
            },
            "raw": dict | Any
        }
        where each item in tool_calls is structured as:
        {
            "id": str,
            "type": "function",
            "function": {
                "name": str,
                "arguments": dict
            }
        }
        """
        pass

    @abstractmethod
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
        """
        Stream chat tokens and tool calls. ``thinking`` as in ``chat``.
        Yields normalized event dictionaries:
        - {"event": "text_delta", "content": str}
        - {"event": "tool_call", "tool_call": dict}  (complete, accumulated per tool call)
        - {"event": "done", "raw": dict}
        - {"event": "error", "message": str}
        """
        pass

    @abstractmethod
    async def unload_model(self, model: Optional[str] = None) -> bool:
        """
        Unload active model(s) from GPU VRAM to yield resources.
        Returns True on success, False otherwise. Must not raise unhandled exceptions.
        """
        pass

    async def count_tokens(self, text: str) -> int:
        """
        Conservative token count estimator (len(text) // 4).
        Documented as an estimate, NOT exact.
        """
        if not text:
            return 0
        return max(1, len(text) // 4)

    async def embeddings(self, texts: list[str]) -> list[list[float]]:
        """Stub for embedding generation."""
        raise NotImplementedError("Embeddings are not implemented for this provider.")

    async def rerank(self, query: str, docs: list[str]) -> list[dict[str, Any]]:
        """Stub for document reranking."""
        raise NotImplementedError("Reranking is not implemented for this provider.")
