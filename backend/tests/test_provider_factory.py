import pytest
from unittest.mock import patch
from app.agent.provider_factory import get_model_provider, reset_provider_cache
from app.agent.llamacpp_provider import LlamaCppProvider


@pytest.fixture(autouse=True)
def clean_provider_cache():
    reset_provider_cache()
    yield
    reset_provider_cache()


def test_returns_llamacpp_provider_for_llama_cpp():
    provider = get_model_provider(runtime="llama_cpp")
    assert isinstance(provider, LlamaCppProvider)
    assert provider.name == "llama_cpp"


def test_removed_fallback_runtimes_are_rejected():
    # Ollama / LM Studio fallbacks were deleted on 2026-09-23; llama.cpp is the only local runtime.
    for runtime in ("ollama", "lmstudio", "hermes3", "bonsai"):
        with pytest.raises(ValueError, match="only local runtime"):
            get_model_provider(runtime=runtime)


def test_raises_for_unknown_runtime():
    with pytest.raises(ValueError, match="Unknown model runtime 'unsupported_runtime'"):
        get_model_provider(runtime="unsupported_runtime")


def test_provider_caching():
    p1 = get_model_provider(runtime="llama_cpp")
    p2 = get_model_provider(runtime="llama_cpp")
    assert p1 is p2

    reset_provider_cache()
    p3 = get_model_provider(runtime="llama_cpp")
    assert p3 is not p1
    assert isinstance(p3, LlamaCppProvider)
