from fake_providers import ClientBackedProvider, use_model_client
import os
import uuid
from pathlib import Path
import pytest
import httpx
from unittest.mock import patch
from app.main import app
from app.agent.tools.read_file import read_file
from app.agent.tools.list_directory import list_directory
from app.agent.tools.registry import execute_tool
from app.agent.model_router import ModelRouter

TEST_SECRET_FILE = "test_sandbox_secret.txt"
TEST_SECRET_CONTENT = "JARVIS_PHASE1_CONFIRMED_40918"


@pytest.fixture(autouse=True)
def setup_test_files(monkeypatch):
    from app.config import settings
    backend_dir = Path("backend").resolve()
    monkeypatch.setattr(settings, "workspace_path", str(backend_dir))
    test_file = backend_dir / TEST_SECRET_FILE
    test_file.write_text(TEST_SECRET_CONTENT, encoding="utf-8")
    yield
    if test_file.exists():
        test_file.unlink(missing_ok=True)


# --- Unit Tests for Tool Functions ---

def test_read_file_success():
    content = read_file(TEST_SECRET_FILE)
    assert content == TEST_SECRET_CONTENT


def test_read_file_not_found():
    result = read_file("non_existent_file_xyz999.txt")
    assert "Error: File not found" in result


def test_read_file_directory():
    result = read_file("app")
    assert "is a directory" in result


def test_list_directory_success():
    result = list_directory("app")
    assert "Contents of 'app' (" in result
    assert "main.py" in result



def test_list_directory_not_found():
    result = list_directory("non_existent_dir_999")
    assert "Error: Directory not found" in result


def test_execute_tool_unregistered():
    result = execute_tool("fake_tool", {})
    assert "Error: Tool 'fake_tool' is not registered" in result


# --- Integration Tests with Mocked Ollama Client ---

@pytest.mark.anyio
@pytest.mark.parametrize("prompt_phrasing", [
    f"Please read the file {TEST_SECRET_FILE} with read_file tool and report its text.",
    f"Please read the contents of {TEST_SECRET_FILE} using read_file tool and tell me the token.",
    f"Please use your read_file tool to inspect {TEST_SECRET_FILE} and report the exact secret code inside it."
])
async def test_tool_calling_read_file_phrasings(prompt_phrasing: str):
    """
    Test tool-calling with 3 different phrasings without loading live models.
    """
    mock_chat_responses = [
        {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "function": {
                            "name": "read_file",
                            "arguments": {"file_path": TEST_SECRET_FILE}
                        }
                    }
                ]
            }
        },
        {
            "message": {
                "role": "assistant",
                "content": f"The secret code is JARVIS_PHASE1_CONFIRMED_40918."
            }
        }
    ]

    class FakeOllama:
        def __init__(self):
            self.idx = 0
        async def chat(self, *args, **kwargs):
            res = mock_chat_responses[self.idx]
            self.idx += 1
            return res

    with use_model_client(FakeOllama()):
        payload = {
            "message": prompt_phrasing,
            "session_id": f"test_phrasing_{uuid.uuid4().hex[:8]}",
            "model": "qwen2.5:0.5b"
        }
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=10.0) as ac:
            response = await ac.post("/chat", json=payload)
        
        assert response.status_code == 200
        data = response.json()
        assert "response" in data
        assert "tools_used" in data
        assert len(data["tools_used"]) >= 1
        assert any(t["tool"] == "read_file" for t in data["tools_used"])
        assert "JARVIS_PHASE1_CONFIRMED_40918" in data["response"] or "JARVIS_PHASE1_CONFIRMED_40918" in str(data["tools_used"])


@pytest.mark.anyio
async def test_tool_calling_list_directory():
    """
    Test tool-calling with directory listing using mocked client.
    """
    mock_chat_responses = [
        {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "function": {
                            "name": "list_directory",
                            "arguments": {"dir_path": "docs"}
                        }
                    }
                ]
            }
        },
        {
            "message": {
                "role": "assistant",
                "content": "The directory contains PLAN.md."
            }
        }
    ]

    class FakeOllama:
        def __init__(self):
            self.idx = 0
        async def chat(self, *args, **kwargs):
            res = mock_chat_responses[self.idx]
            self.idx += 1
            return res

    with use_model_client(FakeOllama()):
        payload = {
            "message": "Please list the files in the 'docs' directory using your list_directory tool.",
            "session_id": f"test_listdir_{uuid.uuid4().hex[:8]}",
            "model": "qwen2.5:0.5b"
        }
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=10.0) as ac:
            response = await ac.post("/chat", json=payload)
        
        assert response.status_code == 200
        data = response.json()
        assert "tools_used" in data
        assert len(data["tools_used"]) >= 1
        assert any(t["tool"] == "list_directory" for t in data["tools_used"])
        assert "PLAN.md" in data["response"] or "PLAN.md" in str(data["tools_used"])


@pytest.mark.anyio
async def test_chat_no_tools_needed():
    """
    Test that general conversation queries do not trigger tools unnecessarily.
    """
    mock_resp = {
        "message": {
            "role": "assistant",
            "content": "Hello! How can I assist you today?"
        }
    }
    class FakeOllama:
        async def chat(self, *args, **kwargs):
            return mock_resp

    with use_model_client(FakeOllama()):
        payload = {
            "message": "Respond with just the greeting 'Hello!' and do not call any tools.",
            "session_id": f"test_notools_{uuid.uuid4().hex[:8]}",
            "model": "qwen2.5:0.5b"
        }
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=10.0) as ac:
            response = await ac.post("/chat", json=payload)
        
        assert response.status_code == 200
        data = response.json()
        assert "tools_used" in data
        assert len(data["tools_used"]) == 0
        assert len(data["response"]) > 0


# --- Stage A Hardening Tests ---

from unittest.mock import AsyncMock
from app.agent.orchestrator import AgentOrchestrator
from app.memory.store import MemoryStore
from app.memory.compactor import ContextCompactor
from app.skills.loader import SkillsLoader
from app.mcp.manager import MCPManager
from app.config import Settings


class MockOllamaClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.idx = 0
        self.chat_calls = []

    async def chat(self, model, messages, tools=None):
        self.chat_calls.append(list(messages))
        if self.idx < len(self.responses):
            res = self.responses[self.idx]
            self.idx += 1
            return res
        return {"message": {"role": "assistant", "content": "Default mock complete"}}


class MockOpenRouterClient:
    def __init__(self, response_text="Escalated Tier 3 completion"):
        self.response_text = response_text
        self.called = False
        self.is_configured = True

    async def chat(self, messages, model):
        self.called = True
        return {
            "choices": [{"message": {"role": "assistant", "content": self.response_text}}]
        }


@pytest.mark.anyio
async def test_malformed_args_rejected_and_repairable(tmp_path):
    """Test malformed tool arguments trigger a validation failure and 1 repair attempt."""
    db_path = str(tmp_path / "test_repair_mem.db")
    store = MemoryStore(db_path=db_path)

    # Iteration 1: Model emits malformed args (missing required 'query' for web_search)
    # Iteration 2: Model corrects args (valid 'query')
    mock_ollama = MockOllamaClient([
        {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "web_search", "arguments": {"invalid_param": "abc"}}}
                ]
            }
        },
        {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "web_search", "arguments": {"query": "python 3.13"}}}
                ]
            }
        },
        {
            "message": {
                "role": "assistant",
                "content": "Here is the search result summary."
            }
        }
    ])
    mock_openrouter = MockOpenRouterClient()
    orchestrator = AgentOrchestrator(
        provider=ClientBackedProvider(mock_ollama),
        openrouter_client=mock_openrouter,
        memory_store=store,
        compactor=ContextCompactor(max_context_tokens=8000),
        skills_loader=SkillsLoader(),
        mcp_manager=MCPManager()
    )

    result = await orchestrator.run(
        user_message="Search for python 3.13",
        session_id=f"test_repair_{uuid.uuid4().hex[:8]}"
    )

    assert result.status == "completed"
    assert len(result.tools_used) == 1
    assert result.tools_used[0]["tool"] == "web_search"

    # Check audit log in store
    audits = store.get_tool_call_audits()
    assert len(audits) >= 2
    # One invalid_repaired, one valid
    val_results = [a["validation_result"] for a in audits]
    assert "invalid_repaired" in val_results
    assert "valid" in val_results


@pytest.mark.anyio
async def test_second_malformed_attempt_escalates_tier(tmp_path):
    """Test 2nd consecutive validation failure on same tool call triggers Tier 3 escalation."""
    db_path = str(tmp_path / "test_esc_mem.db")
    store = MemoryStore(db_path=db_path)

    # Iteration 1: Model emits malformed args (missing query)
    # Iteration 2: Model emits malformed args again (still missing query)
    mock_ollama = MockOllamaClient([
        {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "web_search", "arguments": {"foo": "bar"}}}
                ]
            }
        },
        {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "web_search", "arguments": {"foo": "baz"}}}
                ]
            }
        }
    ])
    mock_openrouter = MockOpenRouterClient("Tier 3 OpenRouter answered successfully")
    orchestrator = AgentOrchestrator(
        provider=ClientBackedProvider(mock_ollama),
        openrouter_client=mock_openrouter,
        memory_store=store,
        compactor=ContextCompactor(max_context_tokens=8000),
        skills_loader=SkillsLoader(),
        mcp_manager=MCPManager()
    )

    result = await orchestrator.run(
        user_message="Search for python",
        session_id=f"test_esc_{uuid.uuid4().hex[:8]}"
    )

    assert result.provider == "openrouter"
    assert "Tier 3 OpenRouter answered" in result.response
    assert mock_openrouter.called is True

    # Audit log should show invalid_escalated
    audits = store.get_tool_call_audits()
    val_results = [a["validation_result"] for a in audits]
    assert "invalid_escalated" in val_results


@pytest.mark.anyio
async def test_duplicate_call_triggers_loop_breaker(tmp_path):
    """Test identical consecutive tool calls trigger loop breaker and halt execution."""
    db_path = str(tmp_path / "test_loop_mem.db")
    store = MemoryStore(db_path=db_path)

    # Model emits exact same fetch_url call in iteration 1 and iteration 2
    mock_ollama = MockOllamaClient([
        {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "fetch_url", "arguments": {"url": "https://example.com"}}}
                ]
            }
        },
        {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "fetch_url", "arguments": {"url": "https://example.com"}}}
                ]
            }
        }
    ])
    orchestrator = AgentOrchestrator(
        provider=ClientBackedProvider(mock_ollama),
        memory_store=store,
        compactor=ContextCompactor(max_context_tokens=8000),
        skills_loader=SkillsLoader(),
        mcp_manager=MCPManager()
    )

    result = await orchestrator.run(
        user_message="Fetch https://example.com",  # a URL the user gave runs without asking
        session_id=f"test_loop_{uuid.uuid4().hex[:8]}"
    )

    assert "attempted the same action twice" in result.response
    assert "stopping to avoid a loop" in result.response
    # Only the first call executed
    assert len(result.tools_used) == 1


@pytest.mark.anyio
async def test_turn_tool_call_cap_enforced(tmp_path, monkeypatch):
    """Test global per-turn tool call cap (MAX_TOOL_CALLS_PER_TURN) halts execution on breach."""
    db_path = str(tmp_path / "test_cap_mem.db")
    store = MemoryStore(db_path=db_path)

    import app.agent.orchestrator as orch_module
    # Temporarily set max tool calls to 2 for deterministic test
    monkeypatch.setattr(orch_module, "MAX_TOOL_CALLS_PER_TURN", 2)

    # Model attempts 3 calls at once
    mock_ollama = MockOllamaClient([
        {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "web_search", "arguments": {"query": "one"}}},
                    {"function": {"name": "web_search", "arguments": {"query": "two"}}},
                    {"function": {"name": "web_search", "arguments": {"query": "three"}}},
                ]
            }
        }
    ])
    orchestrator = AgentOrchestrator(
        provider=ClientBackedProvider(mock_ollama),
        memory_store=store,
        compactor=ContextCompactor(max_context_tokens=8000),
        skills_loader=SkillsLoader(),
        mcp_manager=MCPManager()
    )

    result = await orchestrator.run(
        user_message="Run three searches",
        session_id=f"test_cap_{uuid.uuid4().hex[:8]}"
    )

    assert "Turn tool call limit exceeded" in result.response
    assert len(result.tools_used) == 0


def test_disabled_tools_unregistered():
    """Verify execute_command and delete_file are registered with appropriate permission tiers."""
    from app.agent.tools.registry import TOOL_FUNCTIONS
    from app.agent.permissions import evaluate_tool_permission, RiskTier

    assert "execute_command" in TOOL_FUNCTIONS
    assert "delete_file" in TOOL_FUNCTIONS

    del_perm = evaluate_tool_permission("delete_file", {"file_path": "test.txt"})
    assert del_perm.risk_tier == RiskTier.HIGH_RISK
    assert del_perm.allowed is False

    cmd_perm = evaluate_tool_permission("execute_command", {"command": "rm -rf /"})
    assert cmd_perm.risk_tier == RiskTier.HIGH_RISK
    assert cmd_perm.allowed is False


@pytest.mark.anyio
async def test_tool_call_stats_and_repair_attempt_tracking(tmp_path):
    """Verify tool_call_audit properly distinguishes first-attempt vs repaired calls and calculates stats."""
    db_path = str(tmp_path / "test_stats.db")
    store = MemoryStore(db_path=db_path)

    # 1. Record an immediate first-attempt valid call
    store.record_tool_call_audit(
        call_id="call_1",
        turn_id="turn_1",
        tool_name="web_search",
        args={"query": "python"},
        model_tier="tier2",
        validation_result="valid",
        permission_result="allowed",
        executed=True,
        repair_attempt=0
    )

    # 2. Record a failed first-attempt call (invalid_repaired)
    store.record_tool_call_audit(
        call_id="call_2a",
        turn_id="turn_2",
        tool_name="fetch_url",
        args={},
        model_tier="tier2",
        validation_result="invalid_repaired",
        permission_result="blocked",
        executed=False,
        error="missing url",
        repair_attempt=0
    )

    # 3. Record the successful retry of that call (repair_attempt=1)
    store.record_tool_call_audit(
        call_id="call_2b",
        turn_id="turn_2",
        tool_name="fetch_url",
        args={"url": "https://example.com"},
        model_tier="tier2",
        validation_result="valid",
        permission_result="allowed",
        executed=True,
        repair_attempt=1
    )

    # 4. Record an escalated call
    store.record_tool_call_audit(
        call_id="call_3",
        turn_id="turn_3",
        tool_name="read_file",
        args={},
        model_tier="tier2",
        validation_result="invalid_escalated",
        permission_result="blocked",
        executed=False,
        error="missing file_path",
        repair_attempt=1
    )

    stats = store.get_tool_call_stats(model_tier="tier2")
    assert stats["total_calls"] == 4
    assert stats["first_attempt_valid_count"] == 1
    assert stats["first_attempt_valid_rate"] == 0.25
    assert stats["repaired_valid_count"] == 1
    assert stats["repaired_valid_rate"] == 0.25
    assert stats["escalated_count"] == 1
    assert stats["escalated_rate"] == 0.25
    assert stats["executed_count"] == 2


def test_execute_command_timeout_returns_the_output_so_far(tmp_path, monkeypatch):
    """A command that overruns still hands back what it printed before it was stopped."""
    from app.agent.tools import sample_tools
    monkeypatch.setattr(sample_tools, "COMMAND_TIMEOUT_SECONDS", 4)
    result = sample_tools.execute_command(
        "Write-Output 'first line'; [Console]::Out.Flush(); Start-Sleep -Seconds 30",
        workspace_path=str(tmp_path),
    )
    assert "timed out after 4s" in result
    assert "first line" in result
