import os
import shutil
import pytest
from pathlib import Path

from app.agent.tools.write_file import write_file
from app.agent.tools.patch_file import patch_file
from app.agent.tools.file_search import find_files, grep_in_files
from app.agent.tools.registry import TOOL_FUNCTIONS, AVAILABLE_TOOLS, execute_tool
from app.agent.permissions import evaluate_tool_permission, RiskTier

TEST_DIR = Path("test_sandbox_phase2")


@pytest.fixture(autouse=True)
def cleanup_sandbox(monkeypatch, tmp_path):
    from app.config import settings
    # Work in a temp dir, not the repo: overwrites and patches now leave backups in
    # <workspace>/.jarvis/backups, which used to pile up in the repo root.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "workspace_path", str(Path(".").resolve()))
    if TEST_DIR.exists():
        shutil.rmtree(TEST_DIR, ignore_errors=True)
    TEST_DIR.mkdir(parents=True, exist_ok=True)
    yield
    if TEST_DIR.exists():
        shutil.rmtree(TEST_DIR, ignore_errors=True)


def test_write_file_creation():
    target_file = TEST_DIR / "subfolder" / "sample.py"
    content = "def hello():\n    print('Hello Jarvis')\n"
    
    res = write_file(str(target_file), content)
    assert "Successfully wrote" in res
    assert target_file.exists()
    assert target_file.read_text(encoding="utf-8") == content


def test_write_file_overwrite_prevention():
    target_file = TEST_DIR / "existing.txt"
    target_file.write_text("initial content", encoding="utf-8")

    res = write_file(str(target_file), "new content", overwrite=False)
    assert "already exists and overwrite is set to False" in res
    assert target_file.read_text(encoding="utf-8") == "initial content"


def test_patch_file_success():
    target_file = TEST_DIR / "math_utils.py"
    initial_code = (
        "def add(a, b):\n"
        "    return a + b\n"
        "\n"
        "def multiply(a, b):\n"
        "    return a * b\n"
    )
    target_file.write_text(initial_code, encoding="utf-8")

    search_block = "def add(a, b):\n    return a + b"
    replace_block = "def add(a, b):\n    \"\"\"Add two numbers.\"\"\"\n    return a + b"

    res = patch_file(str(target_file), search_block, replace_block)
    assert "Successfully patched" in res
    
    updated_content = target_file.read_text(encoding="utf-8")
    assert '"""Add two numbers."""' in updated_content
    assert "def multiply(a, b):" in updated_content


def test_patch_file_not_found():
    target_file = TEST_DIR / "test.txt"
    target_file.write_text("Line 1\nLine 2\nLine 3\n", encoding="utf-8")

    res = patch_file(str(target_file), "Nonexistent Line", "Replacement")
    assert "Error: The search block was not found" in res


def test_patch_file_multiple_occurrences():
    target_file = TEST_DIR / "repeat.txt"
    target_file.write_text("item = 10\nitem = 10\n", encoding="utf-8")

    res = patch_file(str(target_file), "item = 10", "item = 20")
    assert "Error: Found 2 occurrences" in res
    assert "must be unique" in res


def test_find_files_glob():
    (TEST_DIR / "a.py").write_text("a", encoding="utf-8")
    (TEST_DIR / "b.py").write_text("b", encoding="utf-8")
    (TEST_DIR / "c.json").write_text("{}", encoding="utf-8")
    (TEST_DIR / "nested").mkdir()
    (TEST_DIR / "nested" / "deep.py").write_text("deep", encoding="utf-8")

    res = find_files("*.py", root_dir=str(TEST_DIR))
    assert "a.py" in res
    assert "b.py" in res
    assert "deep.py" in res
    assert "c.json" not in res


def test_grep_in_files_search():
    (TEST_DIR / "server.py").write_text("def start_server():\n    pass\n", encoding="utf-8")
    (TEST_DIR / "client.py").write_text("def connect_server():\n    pass\n", encoding="utf-8")

    res = grep_in_files("start_server", path=str(TEST_DIR))
    assert "server.py" in res
    assert "L1" in res
    assert "def start_server():" in res


def test_workspace_safety_gating():
    # 1. Local workspace relative path -> auto-allowed LOW_RISK
    dec_local = evaluate_tool_permission("write_file", {"file_path": "scripts/new_script.py"})
    assert dec_local.allowed is True
    assert dec_local.risk_tier == RiskTier.LOW_RISK

    # 2. System critical path -> blocked for confirmation or HIGH_RISK
    dec_sys = evaluate_tool_permission("write_file", {"file_path": "C:/Windows/System32/drivers/etc/hosts"})
    assert dec_sys.allowed is False
    assert dec_sys.risk_tier in (RiskTier.CONFIRMATION_REQUIRED, RiskTier.HIGH_RISK)

    # 3. Patch file on workspace relative path -> auto-allowed
    dec_patch = evaluate_tool_permission("patch_file", {"file_path": "backend/app/main.py"})
    assert dec_patch.allowed is True
    assert dec_patch.risk_tier == RiskTier.LOW_RISK


def test_registry_integration():
    assert "write_file" in TOOL_FUNCTIONS
    assert "patch_file" in TOOL_FUNCTIONS
    assert "find_files" in TOOL_FUNCTIONS
    assert "grep_in_files" in TOOL_FUNCTIONS
    assert write_file in AVAILABLE_TOOLS
    assert patch_file in AVAILABLE_TOOLS
    assert find_files in AVAILABLE_TOOLS
    assert grep_in_files in AVAILABLE_TOOLS

    # Execute via generic execute_tool
    target = str(TEST_DIR / "exec_test.txt")
    write_res = execute_tool("write_file", {"file_path": target, "content": "Jarvis Phase 2 Registry Test"})
    assert "Successfully wrote" in write_res
    assert Path(target).exists()
