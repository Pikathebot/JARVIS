import logging
from typing import Optional
from app.config import settings
from app.agent.model_provider import ModelProvider
from app.agent.llamacpp_provider import LlamaCppProvider
from app.agent.runtime_process_manager import get_runtime_process_manager

logger = logging.getLogger("jarvis.agent.factory")

_cached_providers: dict[str, ModelProvider] = {}


def get_model_provider(runtime: Optional[str] = None) -> ModelProvider:
    """
    Returns the cached local ModelProvider. llama.cpp is the only local runtime; the argument is
    kept because routing decisions carry a provider name ("llama_cpp"). OpenRouter Heavy Mode is
    not a ModelProvider -- the orchestrator talks to its client directly.
    """
    target_runtime = (runtime or settings.model_runtime).strip().lower()
    if target_runtime != "llama_cpp":
        raise ValueError(f"Unknown model runtime '{target_runtime}'. The only local runtime is 'llama_cpp'.")

    if target_runtime in _cached_providers:
        return _cached_providers[target_runtime]

    pm = get_runtime_process_manager()
    provider = LlamaCppProvider(
        base_url=f"http://{settings.llamacpp_host}:{settings.llamacpp_port}",
        process_manager=pm
    )
    _cached_providers[target_runtime] = provider
    logger.info("Initialized ModelProvider: LlamaCppProvider (port %s)", settings.llamacpp_port)
    return provider


def reset_provider_cache() -> None:
    """Clear cached provider instances for testing or reconfiguration."""
    global _cached_providers
    _cached_providers.clear()
