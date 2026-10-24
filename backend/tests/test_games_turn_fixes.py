"""The fixes from the "what games do I have?" turn (PLAN 4.23c, thread 2026-10-23 18:31-18:42)."""
import pytest

from app.agent.orchestrator import _STUCK_REPLY, extract_tool_calls_from_text
from app.agent.permissions import ChatMode
from app.agent.tool_pipeline import ToolPipeline, clean_text
from app.agent.tools import installed
from app.agent.tools.fetch_url import fetch_url
from app.agent.tools.list_directory import list_directory
from app.agent.tools.registry import WORKSPACE_BOUND_TOOLS, list_processes
from tests.test_orchestrator_tool_turns import ScriptedProvider, _orchestrator


def test_lone_surrogates_are_replaced():
    assert clean_text("ok") == "ok"
    cleaned = clean_text("Steam\udc81 folder")
    cleaned.encode("utf-8")
    assert cleaned.startswith("Steam") and cleaned.endswith(" folder")


@pytest.mark.asyncio
async def test_pipeline_cleans_tool_output():
    outcome = await ToolPipeline().run("find_files", {}, native=lambda: "a\udc81b")
    outcome.result.encode("utf-8")


@pytest.mark.parametrize("path", [r"C:\Users\lathu\AppData\Local\Steam\steam.exe", "D:/Games", r"\\nas\share", "file:///C:/x"])
def test_fetch_url_refuses_a_local_path(path):
    result = fetch_url(path)
    assert result.startswith("Error:") and "read_file" in result


def test_qwen_xml_call_is_parsed_whole():
    text = ("Right, let me look at the drives.\n\n<tool_call>\n<function=list_processes>\n"
            "<parameter=filter_name>\nsteam\n</parameter>\n</function>\n</tool_call>")
    cleaned, calls = extract_tool_calls_from_text(text)
    assert cleaned == "Right, let me look at the drives."
    assert calls[0]["function"] == {"name": "list_processes", "arguments": {"filter_name": "steam"}}


def _call(name, args, cid):
    return [
        {"event": "tool_call", "tool_call": {"id": cid, "type": "function", "function": {"name": name, "arguments": args}}},
        {"event": "done", "raw": {"content": "", "reasoning": "", "tool_calls": None}},
    ]


@pytest.mark.asyncio
async def test_a_final_answer_written_as_a_call_is_replaced(tmp_path, monkeypatch):
    # The turn that showed the raw <tool_call>: a call, the same call again (repeat-stopped), then
    # the answer request answered with the call as text.
    as_text = "Let me look again.\n<tool_call>\n<function=list_processes>\n</function>\n</tool_call>"
    provider = ScriptedProvider([
        _call("list_processes", {"filter_name": "steam"}, "a"),
        _call("list_processes", {"filter_name": "steam"}, "b"),
        [{"event": "text_delta", "content": as_text},
         {"event": "done", "raw": {"content": as_text, "reasoning": "", "tool_calls": None}}],
    ])
    orch = _orchestrator()
    events = [ev async for ev in orch._run_provider_stream_loop(
        provider=provider, session_id="games-final", model="main", system_prompt="sys",
        conversation_messages=[{"role": "user", "content": "what games do I have?"}],
        tools=[list_processes], chat_mode=ChatMode.FREEFORM, workspace_path=tmp_path,
    )]
    done = events[-1]["data"]
    assert done["replace_content"] is True
    assert "<tool_call>" not in done["response"] and "function=" not in done["response"]
    # ScriptedProvider.chat answers "prose fallback" to the re-ask without tools.
    assert done["response"] == "prose fallback"


def test_freeform_can_list_a_folder_and_the_drives():
    # back in Freeform 2026-10-24 after the Ornith re-eval (PLAN 4.23c)
    from app.agent.tools.registry import tools_for_space

    assert "list_directory" not in WORKSPACE_BOUND_TOOLS
    assert "list_directory" in [t.__name__ for t in tools_for_space("FREEFORM")]
    assert list_directory("drives").startswith("Drives on this PC")


def test_steam_games_come_from_the_manifests(tmp_path, monkeypatch):
    main, extra = tmp_path / "Steam", tmp_path / "Lib"
    (main / "steamapps").mkdir(parents=True)
    (extra / "steamapps").mkdir(parents=True)
    (main / "steamapps" / "libraryfolders.vdf").write_text(
        '"libraryfolders"\n{\n "0" { "path" "%s" }\n "1" { "path" "%s" }\n}' % (
            str(main).replace("\\", "\\\\"), str(extra).replace("\\", "\\\\")), encoding="utf-8")
    (extra / "steamapps" / "appmanifest_553850.acf").write_text(
        '"AppState"\n{\n "appid" "553850"\n "name" "HELLDIVERS 2"\n "SizeOnDisk" "24159191040"\n}', encoding="utf-8")
    (main / "steamapps" / "appmanifest_228980.acf").write_text(
        '"AppState"\n{\n "appid" "228980"\n "name" "Steamworks Common Redistributables"\n}', encoding="utf-8")
    monkeypatch.setattr(installed, "steam_root", lambda: str(main))
    monkeypatch.setattr(installed, "epic_games", lambda: [])
    monkeypatch.setattr(installed, "gog_games", lambda: [])
    text = installed.installed_games()
    assert "HELLDIVERS 2 (22.5 GB" in text and "Redistributables" not in text
    assert text.startswith("Installed games: 1")


def test_stuck_reply_is_plain():
    assert "<" not in _STUCK_REPLY
