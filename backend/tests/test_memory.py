from fake_providers import ClientBackedProvider, use_model_client
import os
import uuid
import pytest
import httpx
from app.main import app, memory_store, compactor
from app.memory.store import MemoryStore
from app.memory.compactor import (
    ContextCompactor,
    estimate_tokens,
    estimate_messages_tokens,
    prune_tool_outputs,
)


@pytest.fixture
def temp_store(tmp_path):
    db_file = str(tmp_path / "test_memory.db")
    return MemoryStore(db_path=db_file)


# --- Unit Tests for SQLite Store ---

def test_store_session_and_messages(temp_store):
    session = temp_store.get_or_create_session("session_1", title="Test Session")
    assert session["session_id"] == "session_1"

    temp_store.append_message("session_1", role="user", content="Hello Jarvis")
    temp_store.append_message("session_1", role="assistant", content="Hello! How can I help?")

    messages = temp_store.get_messages("session_1")
    assert len(messages) == 2
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "Hello Jarvis"
    assert messages[1]["role"] == "assistant"


def test_store_delete_session(temp_store):
    temp_store.append_message("session_to_del", role="user", content="Test")
    assert len(temp_store.get_messages("session_to_del")) == 1

    deleted = temp_store.delete_session("session_to_del")
    assert deleted is True
    assert len(temp_store.get_messages("session_to_del")) == 0


def test_store_record_compaction(temp_store):
    temp_store.get_or_create_session("sess_comp")
    event = temp_store.record_compaction(
        session_id="sess_comp",
        strategy="tool_pruning",
        tokens_before=500,
        tokens_after=150,
        details="Pruned 2 bulky tool outputs"
    )
    assert event["strategy"] == "tool_pruning"

    events = temp_store.get_compaction_events("sess_comp")
    assert len(events) == 1
    assert events[0]["tokens_before"] == 500
    assert events[0]["tokens_after"] == 150


# --- Unit Tests for Context Compactor ---

def test_token_estimation():
    text = "Hello world! This is a test string."
    tokens = estimate_tokens(text)
    assert tokens == len(text) // 4

    messages = [
        {"role": "user", "content": "Short query"},
        {"role": "assistant", "content": "Short reply"}
    ]
    total_tokens = estimate_messages_tokens(messages)
    assert total_tokens > 0


def test_tool_output_pruning():
    large_tool_content = "FILE CONTENT LINE\n" * 100  # ~1800 chars
    messages = [
        {"role": "user", "content": "Read the file"},
        {"role": "tool", "name": "read_file", "content": large_tool_content},
        {"role": "assistant", "content": "Here is what I found"},
        {"role": "user", "content": "Next request"}
    ]

    pruned, count = prune_tool_outputs(messages, char_threshold=200)
    assert count == 1
    assert "Tool output pruned" in pruned[1]["content"]
    assert len(pruned[1]["content"]) < len(large_tool_content)


# --- Integration Tests with FastAPI app ---

@pytest.mark.anyio
async def test_multi_turn_conversation_memory():
    """Verify that assistant remembers facts stated in prior turns within the same session."""
    session_id = f"test_memory_{uuid.uuid4().hex[:8]}"

    mock_resp1 = {
        "message": {
            "role": "assistant",
            "content": "Understood. I will remember that your favorite programming language is Python 3.11."
        }
    }
    mock_resp2 = {
        "message": {
            "role": "assistant",
            "content": "Your favorite programming language is Python 3.11."
        }
    }

    class FakeOllama:
        def __init__(self):
            self.count = 0
        async def chat(self, *args, **kwargs):
            if self.count == 0:
                self.count += 1
                return mock_resp1
            return mock_resp2

    from unittest.mock import patch
    with use_model_client(FakeOllama()):
        try:
            # Turn 1: User introduces a fact
            turn1_payload = {
                "message": "Remember this fact: My favorite programming language is Python 3.11. Reply with 'Understood'.",
                "session_id": session_id,
                "model": "qwen2.5:0.5b",
                "system_prompt": "You are a concise assistant. Remember facts stated in conversation."
            }
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=10.0) as ac:
                res1 = await ac.post("/chat", json=turn1_payload)
            
            assert res1.status_code == 200
            assert res1.json()["session_id"] == session_id

            # Turn 2: User asks for the stored fact
            turn2_payload = {
                "message": "What is my favorite programming language that I told you?",
                "session_id": session_id,
                "model": "qwen2.5:0.5b",
                "system_prompt": "You are a concise assistant. Remember facts stated in conversation."
            }
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=10.0) as ac:
                res2 = await ac.post("/chat", json=turn2_payload)
            
            assert res2.status_code == 200
            content = res2.json()["response"]
            assert "Python" in content or "3.11" in content

            # Verify messages stored in database
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ac:
                history_res = await ac.get(f"/sessions/{session_id}/messages")
            
            assert history_res.status_code == 200
            messages = history_res.json()
            assert len(messages) >= 4

        finally:
            # Cleanup session
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ac:
                await ac.delete(f"/sessions/{session_id}")


@pytest.mark.anyio
async def test_compaction_triggered_and_logged():
    """Verify that exceeding token limits triggers compaction, logs it, and surfaces in response."""
    session_id = f"test_compaction_{uuid.uuid4().hex[:8]}"

    mock_resp = {
        "message": {
            "role": "assistant",
            "content": "Proceeding with next step."
        }
    }

    class FakeOllama:
        async def chat(self, *args, **kwargs):
            return mock_resp

    from unittest.mock import patch
    with use_model_client(FakeOllama()):
        try:
            # Seed session with bulky tool messages in store
            bulky_text = "DENSE SYSTEM LOG ENTRY DETAILS - " * 50  # ~1700 chars
            memory_store.append_message(session_id, role="user", content="Please inspect the logs")
            memory_store.append_message(session_id, role="tool", content=bulky_text, name="read_file")
            memory_store.append_message(session_id, role="assistant", content="Logs inspected.")

            # Set tight compaction threshold on compactor temporarily
            orig_threshold = compactor.max_context_tokens
            compactor.max_context_tokens = 50  # Force Stage 1 or Stage 2 compaction

            payload = {
                "message": "Continue with the next step.",
                "session_id": session_id,
                "model": "qwen2.5:0.5b"
            }
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=10.0) as ac:
                response = await ac.post("/chat", json=payload)
            
            assert response.status_code == 200
            data = response.json()
            
            # Verify compaction was performed and surfaced
            assert data["compaction_performed"] is not None
            assert "strategy" in data["compaction_performed"]
            assert data["compaction_performed"]["tokens_before"] > data["compaction_performed"]["tokens_after"]

            # Verify compaction event in audit endpoint
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ac:
                audit_res = await ac.get(f"/sessions/{session_id}/compactions")
            
            assert audit_res.status_code == 200
            events = audit_res.json()
            assert len(events) >= 1

        finally:
            compactor.max_context_tokens = orig_threshold
            memory_store.delete_session(session_id)


# --- list_sessions: bounded, and the default workspace owns the unscoped sessions ---

def test_list_sessions_limit_returns_most_recent(temp_store):
    for i in range(6):
        temp_store.append_message(f"lim_{i}", role="user", content=f"m{i}")

    limited = temp_store.list_sessions(limit=3)
    assert len(limited) == 3
    # newest first
    assert [s["session_id"] for s in limited] == ["lim_5", "lim_4", "lim_3"]
    assert len(temp_store.list_sessions()) >= 6


def test_list_sessions_default_workspace_includes_unscoped(temp_store):
    from app.memory.store import DEFAULT_PROJECT_ID

    temp_store.get_or_create_session("unscoped", project_id=None)
    temp_store.get_or_create_session("in_default", project_id=DEFAULT_PROJECT_ID)
    temp_store.get_or_create_session("in_other", project_id="proj-x")

    ids = {s["session_id"] for s in temp_store.list_sessions(project_id=DEFAULT_PROJECT_ID)}
    assert ids == {"unscoped", "in_default"}
    assert {s["session_id"] for s in temp_store.list_sessions(project_id="proj-x")} == {"in_other"}


@pytest.mark.asyncio
async def test_sessions_endpoint_defaults_to_fifty():
    for i in range(55):
        memory_store.append_message(f"ep_{uuid.uuid4().hex[:8]}_{i}", role="user", content="x")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        default = await client.get("/sessions")
        assert default.status_code == 200
        assert len(default.json()) == 50
        everything = await client.get("/sessions", params={"limit": 0})
        assert len(everything.json()) >= 55


def test_compaction_summary_never_swaps_models(monkeypatch):
    """Compaction routes to the fast slot, but a slot switch reloads the weights twice (there
    and back). When main is loaded the summary runs on main with thinking off; when fast is
    loaded it stays on fast; cloud routes and ad-hoc clients are untouched."""
    from unittest.mock import MagicMock
    import app.agent.runtime_process_manager as rpm
    from app.agent.model_provider import ModelProvider
    from app.memory.context_compactor import ContextCompactor

    class Runtime:
        def __init__(self, kind):
            self.current_model_kind = kind

    provider = MagicMock(spec=ModelProvider)
    monkeypatch.setattr(rpm, "get_runtime_process_manager", lambda: Runtime("main"))
    assert ContextCompactor._no_swap_target("fast", "llama_cpp", provider) == ("main", {"thinking": False})
    assert ContextCompactor._no_swap_target("fast", "llama_cpp", object()) == ("main", {})
    assert ContextCompactor._no_swap_target("main", "llama_cpp", provider) == ("main", {})
    assert ContextCompactor._no_swap_target("fast", "openrouter", provider) == ("fast", {})
    monkeypatch.setattr(rpm, "get_runtime_process_manager", lambda: Runtime("fast"))
    assert ContextCompactor._no_swap_target("fast", "llama_cpp", provider) == ("fast", {})
