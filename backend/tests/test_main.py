import pytest
import httpx
from unittest.mock import AsyncMock, patch, MagicMock
from app.main import app
from fake_providers import ClientBackedProvider


@pytest.mark.anyio
async def test_health_check():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert "status" in data
    assert "active_backend" in data


@pytest.mark.anyio
async def test_chat_with_model():
    """Test /chat with mocked backend to avoid loading models in VRAM during tests."""
    from app.agent.llamacpp_provider import LlamaCppProvider
    mock_resp = {
        "message": {
            "role": "assistant",
            "content": "OK",
            "tool_calls": None
        },
        "raw": {}
    }
    with patch.object(LlamaCppProvider, "chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = mock_resp
        payload = {
            "message": "Respond with 'OK' and nothing else.",
            "model": "qwen2.5:0.5b"
        }
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=30.0) as ac:
            response = await ac.post("/chat", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert "response" in data
        assert "OK" in data["response"]
        assert data["model"] == "qwen2.5:0.5b"


@pytest.mark.anyio
async def test_chat_nonexistent_model():
    """Test /chat error handling when provider fails."""
    from app.agent.llamacpp_provider import LlamaCppProvider
    with patch.object(LlamaCppProvider, "chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.side_effect = RuntimeError("model 'non_existent_model_12345:xyz' not found")
        payload = {
            "message": "Hello",
            "model": "non_existent_model_12345:xyz"
        }
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=10.0) as ac:
            response = await ac.post("/chat", json=payload)
        assert response.status_code in (500, 502)
        data = response.json()
        assert "detail" in data


@pytest.mark.anyio
async def test_unload_model_endpoint():
    """Test /models/unload endpoint."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.post("/models/unload", json={"model_name": "qwen2.5:0.5b"})
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True


@pytest.mark.anyio
async def test_chat_stream_endpoint():
    """Test /chat/stream SSE endpoint with mocked orchestrator stream."""
    with patch("app.main.AgentOrchestrator") as MockOrchestrator:
        mock_instance = MagicMock()
        async def mock_run_stream(**kwargs):
            yield {"event": "token", "data": {"delta": "Hello "}}
            yield {"event": "token", "data": {"delta": "world!"}}
            yield {"event": "done", "data": {"response": "Hello world!", "model": "main", "provider": "llama_cpp", "tools_used": []}}
        mock_instance.run_stream = mock_run_stream
        MockOrchestrator.return_value = mock_instance

        payload = {
            "message": "Hello Jarvis",
            "session_id": "test_stream_session"
        }
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ac:
            response = await ac.post("/chat/stream", json=payload)
        assert response.status_code == 200
        assert "text/event-stream" in response.headers.get("content-type", "")
        text = response.text
        assert "event: token" in text
        assert "event: done" in text


@pytest.mark.anyio
async def test_orchestrator_run_stream_conversation():
    """Directly test AgentOrchestrator.run_stream conversational response."""
    from app.agent.orchestrator import AgentOrchestrator
    from app.memory.store import MemoryStore
    from app.memory.compactor import ContextCompactor
    from app.skills.loader import SkillsLoader
    from app.mcp.manager import MCPManager

    mock_client = MagicMock()
    mock_openrouter = MagicMock()
    mock_store = MagicMock()
    mock_store.get_or_create_session.return_value = {"chat_mode": "WORKSPACE"}
    mock_store.get_messages.return_value = []
    
    mock_compactor = MagicMock()
    mock_compactor.compact = AsyncMock(return_value=([{"role": "user", "content": "hi"}], None))
    
    mock_skills = MagicMock()
    mock_skills.match_skills.return_value = []
    mock_skills.build_skill_prompt_injection.return_value = ""

    mock_mcp = MagicMock()
    mock_mcp.get_tool_definitions.return_value = []

    # Scripted streaming client
    async def mock_chat_stream(*args, **kwargs):
        yield {"type": "token", "delta": "Hello "}
        yield {"type": "token", "delta": "there!"}
        yield {"type": "done", "content": "Hello there!", "tool_calls": []}

    mock_client.chat_stream = mock_chat_stream

    orchestrator = AgentOrchestrator(
        provider=ClientBackedProvider(mock_client),
        openrouter_client=mock_openrouter,
        memory_store=mock_store,
        compactor=mock_compactor,
        skills_loader=mock_skills,
        mcp_manager=mock_mcp,
    )

    events = []
    async for ev in orchestrator.run_stream(user_message="hi", session_id="test_conv"):
        events.append(ev)

    event_types = [e.get("event") for e in events]
    assert "token" in event_types
    assert "done" in event_types
    done_ev = next(e for e in events if e.get("event") == "done")
    assert done_ev["data"]["response"] == "Hello there!"


@pytest.mark.anyio
async def test_orchestrator_run_stream_with_tool_call():
    """Directly test AgentOrchestrator.run_stream with tool calls and BatchPermissionResult."""
    from app.agent.orchestrator import AgentOrchestrator

    mock_client = MagicMock()
    mock_openrouter = MagicMock()
    mock_store = MagicMock()
    mock_store.get_or_create_session.return_value = {"chat_mode": "WORKSPACE"}
    mock_store.get_messages.return_value = []
    
    mock_compactor = MagicMock()
    mock_compactor.compact = AsyncMock(return_value=([{"role": "user", "content": "Check disk"}], None))
    
    mock_skills = MagicMock()
    mock_skills.match_skills.return_value = []
    mock_skills.build_skill_prompt_injection.return_value = ""

    mock_mcp = MagicMock()
    mock_mcp.get_tool_definitions.return_value = []

    # Stream returns tool call first, then synthesis
    call_count = 0
    async def mock_chat_stream(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            yield {
                "type": "done",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_123",
                        "function": {
                            "name": "list_directory",
                            "arguments": {"path": "."}
                        }
                    }
                ]
            }
        else:
            yield {"type": "token", "delta": "Here are files."}
            yield {"type": "done", "content": "Here are files.", "tool_calls": []}

    mock_client.chat_stream = mock_chat_stream

    orchestrator = AgentOrchestrator(
        provider=ClientBackedProvider(mock_client),
        openrouter_client=mock_openrouter,
        memory_store=mock_store,
        compactor=mock_compactor,
        skills_loader=mock_skills,
        mcp_manager=mock_mcp,
    )

    events = []
    async for ev in orchestrator.run_stream(user_message="list files in directory", session_id="test_tool"):
        events.append(ev)

    event_types = [e.get("event") for e in events]
    assert "tool_start" in event_types
    assert "tool_end" in event_types
    assert "done" in event_types


