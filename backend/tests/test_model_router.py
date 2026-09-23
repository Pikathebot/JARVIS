from unittest.mock import AsyncMock, patch, MagicMock
import pytest
import httpx
from app.main import app
from app.agent.model_router import ModelRouter, RoutingMode, RoutingDecision, TaskType
from app.agent.openrouter_client import OpenRouterClient
from app.agent.llamacpp_provider import LlamaCppProvider
from app.agent.provider_factory import get_model_provider


def test_route_task_deterministic_fast_model():
    """Verify deterministic routing of simple_chat, summarization, memory, and compaction to FAST_MODEL."""
    router = ModelRouter()

    for task in (TaskType.SIMPLE_CHAT, TaskType.SUMMARIZATION, TaskType.BACKGROUND_MEMORY, TaskType.COMPACTION):
        decision = router.route_task(task_type=task)
        assert decision.model == "fast"
        assert decision.provider == "llama_cpp"
        assert "FAST_MODEL" in decision.reason


def test_route_task_deterministic_main_model():
    """Verify deterministic routing of deep_reasoning, coding, and complex_tool_use to MAIN_MODEL."""
    router = ModelRouter()

    for task in (TaskType.DEEP_REASONING, TaskType.CODING, TaskType.COMPLEX_TOOL_USE):
        decision = router.route_task(task_type=task)
        assert decision.model == "main"
        assert decision.provider == "llama_cpp"
        assert "MAIN_MODEL" in decision.reason


def test_route_task_string_task_type_and_evaluate_kwarg():
    """Verify route_task accepts string task names and evaluate delegates correctly."""
    router = ModelRouter()

    d1 = router.route_task("summarization")
    assert d1.model == "fast"

    d2 = router.route_task("coding")
    assert d2.model == "main"

    d3 = router.evaluate("any prompt", task_type="compaction")
    assert d3.model == "fast"


def test_router_llamacpp_default():
    """Tasks route to the local llama.cpp slots."""
    router = ModelRouter(
        default_mode="auto",
    )
    # Simple message -> fast model
    d1 = router.evaluate("What is the time?")
    assert d1.mode == "normal"
    assert d1.provider == "llama_cpp"
    assert d1.model == "fast"

    # Coding / tool task -> main model
    d2 = router.evaluate("def calculate_fibonacci(n): return n")
    assert d2.mode == "normal"
    assert d2.provider == "llama_cpp"
    assert d2.model == "main"


def test_router_explicit_normal_mode():
    router = ModelRouter(
        default_mode="auto",
    )
    decision = router.evaluate("Design a distributed architecture", requested_mode="normal")
    assert decision.mode == "normal"
    assert decision.provider == "llama_cpp"
    assert decision.model == "main"


def test_router_heavy_mode_disabled_by_default():
    """Verify that heavy mode falls back to local main model when cloud_routing_enabled=False."""
    router = ModelRouter(
        default_mode="auto",
    )
    decision = router.evaluate("[heavy] please explain this quantum equation.", requested_mode="heavy")
    assert decision.mode == "normal"
    assert decision.provider == "llama_cpp"
    assert decision.model == "main"
    assert "cloud routing is disabled" in decision.reason


def test_router_custom_model_override():
    router = ModelRouter(
        default_mode="auto",
    )
    decision = router.evaluate("Hello", requested_mode="normal", requested_model="custom-model.gguf")
    assert decision.model == "custom-model.gguf"


# --- Integration Tests with FastAPI app ---

@pytest.mark.anyio
async def test_chat_llamacpp_normal_mode_mocked():
    """Verify that Normal Mode runs via llama.cpp by default."""
    mock_llamacpp_response = {
        "message": {
            "role": "assistant",
            "content": "Hello from Qwen3.5-9B running locally in llama.cpp.",
            "tool_calls": None
        },
        "raw": {}
    }

    with patch.object(LlamaCppProvider, "chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = mock_llamacpp_response

        payload = {
            "message": "Hello Jarvis",
            "mode": "normal"
        }
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=30.0) as ac:
            response = await ac.post("/chat", json=payload)

        assert response.status_code == 200
        data = response.json()
        assert data["provider"] == "llama_cpp"
        assert "Qwen3.5-9B" in data["response"]
        assert data["fallback_used"] is False


def test_route_task_vision_and_web_extraction():
    """Verify deterministic routing for TaskType.VISION and TaskType.WEB_EXTRACTION (Phase 8)."""
    router = ModelRouter()

    # 1. Vision Task -> VISION_MODEL
    d_vis = router.route_task(TaskType.VISION)
    assert "VISION_MODEL" in d_vis.reason
    assert d_vis.model == "Qwen2.5-VL-7B-Instruct" or "vl" in d_vis.model.lower()

    # 2. Web Extraction Task -> FAST_MODEL
    d_web = router.route_task(TaskType.WEB_EXTRACTION)
    assert "FAST_MODEL" in d_web.reason
    assert d_web.model == "fast"
