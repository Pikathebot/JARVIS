"""Batch 3 of the tool review (2026-10-01): outside-workspace access that works once approved,
backups before overwrite/patch, the Recycle Bin instead of a hard delete, and why on the card."""
from pathlib import Path

import pytest

from app.agent.permissions import RiskTier, evaluate_tool_permission, generate_action_id
from app.agent.tools import file_safety
from app.agent.tools.registry import execute_tool
from app.agent.tools.read_file import read_file
from app.agent.tools.patch_file import patch_file
from app.agent.tools.sample_tools import delete_file
from app.agent.tools.write_file import write_file


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "notes.txt").write_text("outside text", encoding="utf-8")
    return root


# --- outside the workspace: refused unless the gate checked the call ---

def test_outside_paths_need_a_gate_checked_call(ws):
    outside = str(ws.parent / "outside" / "notes.txt")
    assert read_file(outside, workspace_path=str(ws)).startswith("Error: Access denied")
    assert "outside text" in read_file(outside, workspace_path=str(ws), allow_outside=True)

    ctx = {"workspace_path": str(ws), "chat_mode": "WORKSPACE"}
    # A model can't grant itself access...
    out = execute_tool("read_file", {"file_path": outside, "allow_outside": True}, context=ctx)
    assert out.startswith("Error: Access denied")
    # ...the tool loops do, after the permission gate (which asked the user) passed the call.
    out = execute_tool("read_file", {"file_path": outside}, context={**ctx, "permission_checked": True})
    assert "outside text" in out


def test_the_gate_asks_for_outside_paths_and_names_them(ws):
    outside = ws.parent / "outside"
    for tool, args in (("read_file", {"file_path": str(outside / "notes.txt")}),
                       ("list_directory", {"directory_path": str(outside)}),
                       ("grep_in_files", {"pattern": "x", "path": str(outside)})):
        dec = evaluate_tool_permission(tool, args, workspace_path=ws)
        assert dec.allowed is False and "outside this workspace" in dec.reason, tool
        assert str(outside) in dec.reason
    inside = evaluate_tool_permission("list_directory", {"directory_path": "."}, workspace_path=ws)
    assert inside.allowed is True


# --- overwrite asks and is backed up; creating runs; patches are backed up ---

def test_creating_runs_but_replacing_asks(ws):
    assert evaluate_tool_permission("write_file", {"file_path": "new.md", "content": "x"}, workspace_path=ws).allowed
    (ws / "plan.md").write_text("v1", encoding="utf-8")
    dec = evaluate_tool_permission("write_file", {"file_path": "plan.md", "content": "v2"}, workspace_path=ws)
    assert dec.allowed is False and "Replaces the existing 'plan.md'" in dec.reason


def test_write_and_patch_back_up_the_previous_version(ws):
    (ws / "plan.md").write_text("v1", encoding="utf-8")
    out = write_file("plan.md", "v2", workspace_path=str(ws))
    assert "previous version is saved as '.jarvis/backups/plan.md." in out
    out = patch_file("plan.md", "v2", "v3", workspace_path=str(ws))
    assert "previous version is saved" in out
    backups = sorted((ws / ".jarvis" / "backups").glob("plan.md.*.bak"))
    assert [b.read_text(encoding="utf-8") for b in backups] == ["v1", "v2"]
    assert (ws / "plan.md").read_text(encoding="utf-8") == "v3"
    # A brand-new file has nothing to back up.
    assert "previous version" not in write_file("fresh.md", "x", workspace_path=str(ws))


def test_backups_keep_only_the_newest_copies(ws, monkeypatch):
    monkeypatch.setattr(file_safety, "KEEP_PER_FILE", 3)
    target = ws / "a.txt"
    for i in range(6):
        target.write_text(str(i), encoding="utf-8")
        file_safety.backup_file(target, ws)
    kept = sorted((ws / ".jarvis" / "backups").glob("a.txt.*.bak"))
    assert [b.read_text(encoding="utf-8") for b in kept] == ["3", "4", "5"]


# --- delete: the Recycle Bin, folders too, never the workspace itself ---

def test_delete_moves_files_and_folders_to_the_recycle_bin(ws, monkeypatch):
    binned = []

    def fake_bin(path: Path):
        binned.append(path)
        import shutil
        shutil.rmtree(path) if path.is_dir() else path.unlink()

    monkeypatch.setattr(file_safety, "send_to_recycle_bin", fake_bin)
    (ws / "old.txt").write_text("x", encoding="utf-8")
    (ws / "drafts").mkdir()
    for n in ("a.md", "b.md"):
        (ws / "drafts" / n).write_text("x", encoding="utf-8")

    card = evaluate_tool_permission("delete_file", {"file_path": "drafts"}, workspace_path=ws)
    assert card.allowed is False and "2 file(s)" in card.reason and "Recycle Bin" in card.reason

    assert delete_file("old.txt", workspace_path=str(ws)) == "Moved 'old.txt' to the Recycle Bin."
    assert delete_file("drafts", workspace_path=str(ws)).startswith("Moved the folder 'drafts' (2 files)")
    assert [p.name for p in binned] == ["old.txt", "drafts"]
    assert delete_file(".", workspace_path=str(ws)).startswith("Error: That is the workspace itself")


# --- the model's reason rides on the card, and doesn't change the approval id ---

def test_the_models_reason_is_on_the_card_but_not_in_the_action_id(ws):
    args = {"command": "npm install", "reason": "the user asked to set up the project"}
    dec = evaluate_tool_permission("execute_command", args, workspace_path=ws)
    assert dec.allowed is False and "Jarvis: the user asked to set up the project" in dec.reason
    reworded = {"command": "npm install", "reason": "installing dependencies as asked"}
    assert generate_action_id("execute_command", args) == generate_action_id("execute_command", reworded)
    assert RiskTier.LOW_RISK != dec.risk_tier
