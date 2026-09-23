from fake_providers import ClientBackedProvider, use_model_client
import os
import uuid
import pytest
import httpx
from unittest.mock import patch
from app.main import app
from app.agent.permissions import (
    RiskTier,
    BASE_TOOL_RISK_MAP,
    evaluate_tool_permission,
    evaluate_tool_calls_batch,
    generate_action_id,
    evaluate_command_argument_risk,
    evaluate_file_path_risk,
)


# --- Unit Tests for Deterministic Hardcoded Lookup ---

def test_low_risk_tool_auto_allowed():
    decision = evaluate_tool_permission("read_file", {"file_path": "docs/PLAN.md"})
    assert decision.allowed is True
    assert decision.risk_tier == RiskTier.LOW_RISK


def test_confirmation_required_tool_blocked_by_default():
    decision = evaluate_tool_permission("execute_command", {"command": "npm install"})
    assert decision.allowed is False
    assert decision.risk_tier == RiskTier.CONFIRMATION_REQUIRED
    assert decision.action_id.startswith("act_")


def test_high_risk_tool_blocked_by_default():
    decision = evaluate_tool_permission("delete_file", {"file_path": "some_file.txt"})
    assert decision.allowed is False
    assert decision.risk_tier == RiskTier.HIGH_RISK
    assert decision.action_id.startswith("act_")


def test_unclassified_tool_defaults_to_confirmation_required():
    """Unclassified tools MUST default to CONFIRMATION_REQUIRED, never LOW_RISK."""
    decision = evaluate_tool_permission("mysterious_new_tool", {"param": "val"})
    assert decision.allowed is False
    assert decision.risk_tier == RiskTier.CONFIRMATION_REQUIRED


def test_argument_aware_command_risk():
    # Safe commands downgraded to LOW_RISK
    assert evaluate_command_argument_risk("git status") == RiskTier.LOW_RISK
    assert evaluate_command_argument_risk("dir") == RiskTier.LOW_RISK
    assert evaluate_command_argument_risk("echo hello") == RiskTier.LOW_RISK
    
    # Destructive commands escalated to HIGH_RISK
    assert evaluate_command_argument_risk("rm -rf /") == RiskTier.HIGH_RISK
    assert evaluate_command_argument_risk("del /f important.db") == RiskTier.HIGH_RISK
    assert evaluate_command_argument_risk("format C:") == RiskTier.HIGH_RISK
    
    # Unknown commands remain CONFIRMATION_REQUIRED
    assert evaluate_command_argument_risk("curl http://example.com") == RiskTier.CONFIRMATION_REQUIRED


def test_argument_aware_file_path_risk():
    assert evaluate_file_path_risk("docs/PLAN.md") == RiskTier.LOW_RISK
    assert evaluate_file_path_risk(r"C:\Windows\System32\drivers\etc\hosts") == RiskTier.HIGH_RISK
    assert evaluate_file_path_risk(r"C:\Program Files\App\config.json") == RiskTier.HIGH_RISK


def test_approval_token_grants_permission():
    args = {"file_path": "sample.txt"}
    action_id = generate_action_id("delete_file", args)
    
    # Denied without token
    denied = evaluate_tool_permission("delete_file", args)
    assert denied.allowed is False
    
    # Allowed with matching token
    approved = evaluate_tool_permission("delete_file", args, approved_action_ids=[action_id])
    assert approved.allowed is True
    assert approved.action_id == action_id


def test_modifying_tier_map_changes_behavior_deterministically():
    """Prove that hardcoded lookup table drives decision, not model prompts."""
    original_tier = BASE_TOOL_RISK_MAP.get("read_file")
    try:
        # Reclassify read_file to HIGH_RISK
        BASE_TOOL_RISK_MAP["read_file"] = RiskTier.HIGH_RISK
        decision = evaluate_tool_permission("read_file", {"file_path": "docs/PLAN.md"})
        assert decision.allowed is False
        assert decision.risk_tier == RiskTier.HIGH_RISK
    finally:
        # Restore original tier
        BASE_TOOL_RISK_MAP["read_file"] = original_tier


def test_batch_tool_permission_evaluation():
    tool_calls = [
        {"name": "read_file", "args": {"file_path": "a.txt"}},
        {"name": "delete_file", "args": {"file_path": "b.txt"}},
    ]
    batch = evaluate_tool_calls_batch(tool_calls)
    assert batch.all_allowed is False
    assert len(batch.approved_actions) == 1
    assert len(batch.pending_confirmations) == 1
    assert batch.pending_confirmations[0].tool == "delete_file"


# --- Integration Tests with Mocked LLM ---

class MockPermissionOllamaClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.idx = 0

    async def chat(self, model, messages, tools=None):
        if self.idx < len(self.responses):
            res = self.responses[self.idx]
            self.idx += 1
            return res
        return {"message": {"role": "assistant", "content": "Done processing."}}


@pytest.mark.anyio
async def test_integration_low_risk_auto_executes():
    """LOW-RISK tool calls should execute without confirmation status."""
    test_file = "test_lowrisk_sample.txt"
    with open(test_file, "w", encoding="utf-8") as f:
        f.write("LOW_RISK_CONFIRMED_DATA")

    mock_client = MockPermissionOllamaClient([
        {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "read_file", "arguments": {"file_path": test_file}}}
                ]
            }
        },
        {
            "message": {
                "role": "assistant",
                "content": "File read successfully: LOW_RISK_CONFIRMED_DATA"
            }
        }
    ])

    try:
        payload = {
            "message": f"Please read the file {test_file} using read_file and report its contents.",
            "session_id": f"test_lowrisk_{uuid.uuid4().hex[:8]}",
            "model": "hermes3:8b"
        }
        with use_model_client(mock_client):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=45.0) as ac:
                response = await ac.post("/chat", json=payload)
        
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "completed"
        assert len(data["pending_confirmations"]) == 0
        assert len(data["tools_used"]) >= 1
        assert any(t["tool"] == "read_file" for t in data["tools_used"])
    finally:
        if os.path.exists(test_file):
            os.remove(test_file)


@pytest.mark.anyio
async def test_integration_confirmation_required_blocks_and_resumes(monkeypatch, tmp_path):
    """Test that a confirmation-required tool is intercepted, returns an action ID, and runs when confirmed."""
    from app.config import settings
    from app.agent.permissions import BASE_TOOL_RISK_MAP, RiskTier

    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "workspace_path", str(workspace_dir))
    monkeypatch.setitem(BASE_TOOL_RISK_MAP, "write_file", RiskTier.CONFIRMATION_REQUIRED)

    test_file = str(workspace_dir / "test_confirm.txt")
    if os.path.exists(test_file):
        os.remove(test_file)

    session_id = f"test_confirm_{uuid.uuid4().hex[:8]}"

    # Turn 1: Model requests write_file (requires confirmation)
    mock_turn1 = MockPermissionOllamaClient([
        {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "write_file", "arguments": {"file_path": test_file, "content": "temporary file"}}}
                ]
            }
        }
    ])

    # Turn 2: User passes approved_action_ids, tool executes and returns
    mock_turn2 = MockPermissionOllamaClient([
        {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "write_file", "arguments": {"file_path": test_file, "content": "temporary file"}}}
                ]
            }
        },
        {
            "message": {
                "role": "assistant",
                "content": "File has been written."
            }
        }
    ])

    from app.database.session import SessionLocal
    from app.database.models import Project
    with SessionLocal() as db:
        test_proj = Project(
            id="test_perm_proj",
            name="Perm Test Project",
            workspace_path=str(workspace_dir),
            is_active=False
        )
        db.merge(test_proj)
        db.commit()

    try:
        # 1. First call: Model attempts write_file (requires confirmation)
        initial_payload = {
            "message": f"Write 'temporary file' to the file '{test_file}' using write_file.",
            "session_id": session_id,
            "project_id": "test_perm_proj",
            "model": "hermes3:8b"
        }
        with use_model_client(mock_turn1):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=45.0) as ac:
                res1 = await ac.post("/chat", json=initial_payload)
        
        assert res1.status_code == 200
        data1 = res1.json()
        assert data1["status"] == "confirmation_required"
        assert len(data1["pending_confirmations"]) > 0
        action = data1["pending_confirmations"][0]
        assert action["tool"] == "write_file"
        action_id = action["action_id"]
        
        # Verify file was NOT written yet
        assert not os.path.exists(test_file)

        # 2. Second call: Provide approved_action_ids token to approve execution
        approved_payload = {
            "message": f"Write 'temporary file' to the file '{test_file}' using write_file.",
            "session_id": session_id,
            "project_id": "test_perm_proj",
            "model": "hermes3:8b",
            "approved_action_ids": [action_id]
        }
        with use_model_client(mock_turn2):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=45.0) as ac:
                res2 = await ac.post("/chat", json=approved_payload)
        
        assert res2.status_code == 200
        data2 = res2.json()
        assert data2["status"] == "completed"
        assert any(t["tool"] == "write_file" for t in data2["tools_used"])
        # Verify file WAS written
        assert os.path.exists(test_file)
    finally:
        if os.path.exists(test_file):
            os.remove(test_file)
        with SessionLocal() as db:
            p_to_del = db.get(Project, "test_perm_proj")
            if p_to_del:
                db.delete(p_to_del)
                db.commit()

        # Cleanup session
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ac:
            await ac.delete(f"/sessions/{session_id}")


# --- Stage A Hardening Tests ---

def test_confirmation_timeout_denies_by_default():
    from app.agent.permissions import CONFIRMATION_TIMEOUT_ACTION
    from app.config import settings
    assert CONFIRMATION_TIMEOUT_ACTION == "deny"
    assert settings.confirmation_timeout_action == "deny"


def test_rate_limit_per_tool_enforced():
    from app.agent.permissions import RATE_LIMITS, check_rate_limit
    original_limits = dict(RATE_LIMITS)
    try:
        RATE_LIMITS["web_search"] = 2
        assert check_rate_limit("web_search", 0) is True
        assert check_rate_limit("web_search", 1) is True
        assert check_rate_limit("web_search", 2) is False
        assert check_rate_limit("web_search", 3) is False

        # Unrestricted tools pass
        assert check_rate_limit("fetch_url", 10) is True
    finally:
        RATE_LIMITS.clear()
        RATE_LIMITS.update(original_limits)


def test_action_token_reverified_on_each_call_in_chain():
    """Verify action token approval on one tool call does not leak to subsequent calls in chain."""
    call1 = {"name": "execute_command", "args": {"command": "git push origin main"}}
    call2 = {"name": "delete_file", "args": {"file_path": "production.sqlite3"}}

    token1 = generate_action_id(call1["name"], call1["args"])

    # Batch with token for call1 only
    batch = evaluate_tool_calls_batch([call1, call2], approved_action_ids=[token1])
    assert batch.all_allowed is False
    assert len(batch.approved_actions) == 1
    assert batch.approved_actions[0].action_id == token1
    assert len(batch.pending_confirmations) == 1
    assert batch.pending_confirmations[0].tool == "delete_file"


def test_path_traversal_blocked(tmp_path):
    # Case 1: Relative path traversal escape
    traversal_path = "../../some_system_file.txt"
    assert evaluate_file_path_risk(traversal_path) == RiskTier.CONFIRMATION_REQUIRED

    # Case 2: Absolute path outside workspace
    outside_abs_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../outside.txt"))
    assert evaluate_file_path_risk(outside_abs_path) == RiskTier.CONFIRMATION_REQUIRED

    # Case 3: Symlink escape outside workspace
    outside_target = tmp_path / "outside_target.txt"
    outside_target.write_text("secret outside")
    symlink_path = os.path.join(os.path.dirname(__file__), "test_symlink_escape.txt")
    try:
        try:
            os.symlink(str(outside_target), symlink_path)
            assert evaluate_file_path_risk(symlink_path) == RiskTier.CONFIRMATION_REQUIRED
        except (OSError, NotImplementedError):
            # If OS privileges restrict symlink creation on Windows without Developer Mode,
            # verify path outside workspace directly via resolve
            assert evaluate_file_path_risk(str(outside_target)) == RiskTier.CONFIRMATION_REQUIRED
    finally:
        if os.path.islink(symlink_path) or os.path.exists(symlink_path):
            os.remove(symlink_path)


# --- Stage A Follow-Up Tests (ChatMode & Approval Bypass Fix) ---

def test_approval_token_strictly_per_action_no_bypass():
    """Verify that approving action 1 does NOT leak approval to a different action with same tool or filename."""
    from app.agent.permissions import evaluate_tool_permission, generate_action_id, ChatMode

    args1 = {"command": "npm test"}
    token1 = generate_action_id("execute_command", args1)

    # 1. Action 1 with its own token -> approved
    dec1 = evaluate_tool_permission("execute_command", args1, approved_action_ids=[token1])
    assert dec1.allowed is True

    # 2. Action 2 with different args -> NOT approved despite matching tool name
    args2 = {"command": "rm -rf /"}
    dec2 = evaluate_tool_permission("execute_command", args2, approved_action_ids=[token1])
    assert dec2.allowed is False

    # 3. Passing tool_name in approved_ids does NOT bypass
    dec3 = evaluate_tool_permission("execute_command", args2, approved_action_ids=["execute_command"])
    assert dec3.allowed is False

    # 4. Passing filename in approved_ids does NOT bypass
    write_args1 = {"file_path": "C:/outside/target.py", "content": "print(1)"}
    write_args2 = {"file_path": "D:/other/target.py", "content": "print(2)"}
    dec_file = evaluate_tool_permission("write_file", write_args2, approved_action_ids=["target.py"], chat_mode=ChatMode.WORKSPACE)
    assert dec_file.allowed is False


def test_workspace_mode_outside_root_requires_confirmation():
    from app.agent.permissions import evaluate_file_path_risk, ChatMode, RiskTier
    outside_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../external_file.txt"))
    assert evaluate_file_path_risk(outside_path, is_write_or_delete=False, chat_mode=ChatMode.WORKSPACE) == RiskTier.CONFIRMATION_REQUIRED
    assert evaluate_file_path_risk(outside_path, is_write_or_delete=True, chat_mode=ChatMode.WORKSPACE) == RiskTier.HIGH_RISK


def test_system_mode_outside_root_low_risk():
    from app.agent.permissions import evaluate_file_path_risk, ChatMode, RiskTier
    outside_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../external_file.txt"))
    assert evaluate_file_path_risk(outside_path, is_write_or_delete=False, chat_mode=ChatMode.SYSTEM) == RiskTier.LOW_RISK
    assert evaluate_file_path_risk(outside_path, is_write_or_delete=True, chat_mode=ChatMode.SYSTEM) == RiskTier.LOW_RISK


def test_system_mode_still_blocks_system_critical_dirs():
    from app.agent.permissions import evaluate_file_path_risk, ChatMode, RiskTier
    assert evaluate_file_path_risk("C:/Windows/System32/drivers/etc/hosts", chat_mode=ChatMode.SYSTEM) == RiskTier.HIGH_RISK
    assert evaluate_file_path_risk("/etc/shadow", chat_mode=ChatMode.SYSTEM) == RiskTier.HIGH_RISK
    assert evaluate_file_path_risk("C:/Program Files/critical.dll", chat_mode=ChatMode.SYSTEM) == RiskTier.HIGH_RISK


def test_unset_mode_defaults_to_workspace_strictness():
    from app.agent.permissions import evaluate_file_path_risk, RiskTier
    outside_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../external_file.txt"))
    # Default without chat_mode passed must be WORKSPACE (stricter)
    assert evaluate_file_path_risk(outside_path) == RiskTier.CONFIRMATION_REQUIRED


def test_write_file_respects_chat_mode():
    from app.agent.permissions import evaluate_tool_permission, ChatMode, RiskTier
    outside_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../external_file.txt"))

    # WORKSPACE mode -> blocked/HIGH_RISK
    dec_ws = evaluate_tool_permission("write_file", {"file_path": outside_path, "content": "test"}, chat_mode=ChatMode.WORKSPACE)
    assert dec_ws.allowed is False
    assert dec_ws.risk_tier == RiskTier.HIGH_RISK

    # SYSTEM mode -> allowed/LOW_RISK
    dec_sys = evaluate_tool_permission("write_file", {"file_path": outside_path, "content": "test"}, chat_mode=ChatMode.SYSTEM)
    assert dec_sys.allowed is True
    assert dec_sys.risk_tier == RiskTier.LOW_RISK


def test_patch_file_respects_chat_mode():
    from app.agent.permissions import evaluate_tool_permission, ChatMode, RiskTier
    outside_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../external_file.txt"))

    # WORKSPACE mode -> blocked
    dec_ws = evaluate_tool_permission("patch_file", {"file_path": outside_path, "search_block": "a", "replacement_block": "b"}, chat_mode=ChatMode.WORKSPACE)
    assert dec_ws.allowed is False
    assert dec_ws.risk_tier == RiskTier.HIGH_RISK

    # SYSTEM mode -> allowed
    dec_sys = evaluate_tool_permission("patch_file", {"file_path": outside_path, "search_block": "a", "replacement_block": "b"}, chat_mode=ChatMode.SYSTEM)
    assert dec_sys.allowed is True
    assert dec_sys.risk_tier == RiskTier.LOW_RISK


# --- Voice-driven confirmation prompts ---


def test_confirmation_prompt_is_empty_for_no_pending_actions():
    from app.agent.permissions import build_confirmation_prompt
    from app.persona.profiles import JARVIS

    assert build_confirmation_prompt([], JARVIS) == {"text": "", "spoken": ""}


def test_confirmation_prompt_describes_a_single_action_and_asks_yes_no():
    from app.agent.permissions import build_confirmation_prompt
    from app.persona.profiles import JARVIS

    pending = [{
        "action_id": "act_1",
        "tool": "execute_command",
        "args": {"command": "git push origin main"},
        "risk_tier": "CONFIRMATION_REQUIRED",
        "reason": "requires confirmation",
    }]
    prompt = build_confirmation_prompt(pending, JARVIS)

    assert "git push origin main" in prompt["spoken"]
    assert "yes to proceed" in prompt["spoken"]
    assert "no to cancel" in prompt["spoken"]
    assert prompt["spoken"].endswith(", sir.")
    assert "**Confirmation required**" in prompt["text"]


def test_confirmation_prompt_summarizes_multiple_actions():
    from app.agent.permissions import build_confirmation_prompt
    from app.persona.profiles import ASSISTANT

    pending = [
        {"action_id": "act_1", "tool": "execute_command", "args": {"command": "npm install"}, "risk_tier": "CONFIRMATION_REQUIRED"},
        {"action_id": "act_2", "tool": "kill_process", "args": {"process_name": "notepad.exe"}, "risk_tier": "CONFIRMATION_REQUIRED"},
    ]
    prompt = build_confirmation_prompt(pending, ASSISTANT)

    assert "2 actions" in prompt["spoken"]
    assert "npm install" in prompt["spoken"]
    assert "notepad.exe" in prompt["spoken"]
    # ASSISTANT has no address term, so no persona-specific sign-off.
    assert not prompt["spoken"].endswith(", sir.")
    assert prompt["text"].count("- ") == 2


def test_confirmation_prompt_falls_back_to_tool_name_without_recognizable_args():
    from app.agent.permissions import build_confirmation_prompt
    from app.persona.profiles import JARVIS

    pending = [{"action_id": "act_1", "tool": "get_clipboard", "args": {}, "risk_tier": "CONFIRMATION_REQUIRED"}]
    prompt = build_confirmation_prompt(pending, JARVIS)

    assert "get clipboard" in prompt["spoken"]




# --- launch_app: opening an installed app by name is not a risky action ---

def test_launch_app_by_name_is_low_risk_but_paths_and_scripts_still_ask(monkeypatch):
    from app.agent import permissions as perm
    from app.agent.tools import app_control

    monkeypatch.setattr(app_control, "find_start_menu_shortcut",
                        lambda name: r"C:\Users\x\Start Menu\Programs\Discord.lnk" if name.lower() == "discord" else None)

    assert perm.evaluate_launch_app_risk("settings")[0] == perm.RiskTier.LOW_RISK
    assert perm.evaluate_launch_app_risk("discord")[0] == perm.RiskTier.LOW_RISK
    assert perm.evaluate_launch_app_risk("notepad")[0] == perm.RiskTier.LOW_RISK
    assert perm.evaluate_launch_app_risk("vs code")[0] == perm.RiskTier.LOW_RISK
    assert perm.evaluate_launch_app_risk("no-such-app-anywhere")[0] == perm.RiskTier.LOW_RISK
    assert perm.evaluate_launch_app_risk(r"C:\Users\x\Downloads\setup.exe")[0] == perm.RiskTier.CONFIRMATION_REQUIRED
    assert perm.evaluate_launch_app_risk("D:/JARVIS/Jarvis.bat")[0] == perm.RiskTier.CONFIRMATION_REQUIRED
    assert perm.evaluate_launch_app_risk("")[0] == perm.RiskTier.CONFIRMATION_REQUIRED

    decision = perm.evaluate_tool_permission("launch_app", {"name_or_path": "discord"})
    assert decision.allowed and decision.risk_tier == perm.RiskTier.LOW_RISK
    decision = perm.evaluate_tool_permission("launch_app", {"name_or_path": r"C:\tmp\thing.exe"})
    assert not decision.allowed
