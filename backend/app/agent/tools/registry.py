import inspect
import logging
from typing import Callable, Any, Literal, Optional
from pydantic import BaseModel, Field

from app.agent.tools.read_file import read_file
from app.agent.tools.list_directory import list_directory
from app.agent.tools.sample_tools import execute_command, delete_file
from app.agent.tools.web_search import web_search
from app.agent.tools.fetch_url import fetch_url
from app.agent.tools.write_file import write_file
from app.agent.tools.patch_file import patch_file
from app.agent.tools.file_search import find_files, grep_in_files
from app.agent.tools.app_control import launch_app, focus_app
from app.agent.tools.media_control import set_volume, set_mute, media_control
from app.agent.tools.clipboard_control import get_clipboard, set_clipboard
from app.agent.tools.process_control import list_processes, kill_process
from app.agent.tools.reminders import remind_me, reminders
from app.agent.tools.artifacts import create_artifact, update_artifact, patch_artifact, read_artifact
from app.agent.tools.system_status import get_system_status
from app.agent.tools.git import git_status, git_diff, git_log, git_commit, git_switch, git_restore, git_push, git_pull
from app.agent.tools.memory import remember, forget, MEMORY_KINDS

logger = logging.getLogger("jarvis.agent.tools")

TOOL_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "read_file": read_file,
    "list_directory": list_directory,
    "web_search": web_search,
    "fetch_url": fetch_url,
    "write_file": write_file,
    "patch_file": patch_file,
    "find_files": find_files,
    "grep_in_files": grep_in_files,
    "execute_command": execute_command,
    "delete_file": delete_file,
    # Artifact Tools (Build Plan §15)
    "create_artifact": create_artifact,
    "update_artifact": update_artifact,
    "patch_artifact": patch_artifact,
    "read_artifact": read_artifact,
    # Phase 3 OS Tools
    "launch_app": launch_app,
    "focus_app": focus_app,
    "set_volume": set_volume,
    "set_mute": set_mute,
    "media_control": media_control,
    "get_clipboard": get_clipboard,
    "set_clipboard": set_clipboard,
    "list_processes": list_processes,
    "kill_process": kill_process,
    "remind_me": remind_me,
    "reminders": reminders,
    "get_system_status": get_system_status,
    # Git (ported from the never-offered app/tools BaseTool stack)
    "git_status": git_status,
    "git_diff": git_diff,
    "git_log": git_log,
    "git_commit": git_commit,
    "git_switch": git_switch,
    "git_restore": git_restore,
    "git_push": git_push,
    "git_pull": git_pull,
    # Long-term memory the model keeps for itself
    "remember": remember,
    "forget": forget,
}

AVAILABLE_TOOLS: list[Callable[..., Any]] = [
    read_file,
    list_directory,
    web_search,
    fetch_url,
    write_file,
    patch_file,
    find_files,
    grep_in_files,
    execute_command,
    delete_file,
    # Artifact Tools
    create_artifact,
    update_artifact,
    patch_artifact,
    read_artifact,
    # Phase 3 OS Tools
    launch_app,
    focus_app,
    set_volume,
    set_mute,
    media_control,
    get_clipboard,
    set_clipboard,
    list_processes,
    kill_process,
    remind_me,
    reminders,
    # No play_audio/stop_playback: Jarvis's speech is played by the client, so the backend has
    # no audio of its own for a model to start or stop (removed 2026-09-26).
    get_system_status,
    # Git
    git_status,
    git_diff,
    git_log,
    git_commit,
    git_switch,
    git_restore,
    git_push,
    git_pull,
    # Memory
    remember,
    forget,
]


class ReadFileArgs(BaseModel):
    file_path: str = Field(..., description="Path to the file to read")
    start_line: Optional[int] = Field(default=None, description="First line to return (1-based). For long files read a part at a time; omit both to read the whole file")
    end_line: Optional[int] = Field(default=None, description="Last line to return (inclusive)")


class ListDirectoryArgs(BaseModel):
    directory_path: str = Field(default=".", description="Path of directory to list")


class ExecuteCommandArgs(BaseModel):
    command: str = Field(..., description="Shell command string to execute")
    reason: str = Field(default="", description="Why, in a few words -- shown to the user on the approval card")


class DeleteFileArgs(BaseModel):
    file_path: str = Field(..., description="Path of the file or folder to delete")
    reason: str = Field(default="", description="Why, in a few words -- shown to the user on the approval card")


class WebSearchArgs(BaseModel):
    query: str = Field(..., description="Short search terms")
    max_results: int = Field(default=5, ge=1, le=10, description="Max results")
    news: bool = Field(default=False, description="Search recent news articles (dated, with source) instead of web pages")


class FetchUrlArgs(BaseModel):
    url: str = Field(..., description="Web URL to fetch")
    max_chars: int = Field(default=8000, ge=500, le=25000, description="Max character budget")


class WriteFileArgs(BaseModel):
    file_path: str = Field(..., description="Target file path")
    content: str = Field(..., description="Content to write")
    overwrite: bool = Field(default=True, description="Overwrite if exists")
    reason: str = Field(default="", description="Why, in a few words -- shown to the user on the approval card")


class PatchFileArgs(BaseModel):
    file_path: str = Field(..., description="Target file path")
    search_block: str = Field(..., description="Exact code block to replace")
    replacement_block: str = Field(..., description="New code block")
    reason: str = Field(default="", description="Why, in a few words -- shown to the user on the approval card")


class FindFilesArgs(BaseModel):
    pattern: str = Field(..., description="File pattern or extension")
    root_dir: str = Field(default=".", description="Root search directory")
    max_results: int = Field(default=50, description="Maximum number of matches to return")


class GrepInFilesArgs(BaseModel):
    pattern: str = Field(..., description="Regex pattern or keyword")
    path: str = Field(default=".", description="Directory path")
    max_matches: int = Field(default=50, ge=1, le=200, description="Max match count")
    case_sensitive: bool = Field(default=False, description="Case sensitivity")


class CreateArtifactArgs(BaseModel):
    name: str = Field(..., description="Title or filename for the artifact (e.g. 'main.py', 'system_architecture.md')")
    type: str = Field(default="code", description="Artifact type ('code', 'markdown', 'html', 'json', 'csv', 'python', 'svg', 'document', 'other')")
    content: str = Field(..., description="Full text or code content of the artifact")
    language: Optional[str] = Field(default=None, description="Programming or markup language (e.g. 'python', 'typescript', 'markdown')")
    summary: Optional[str] = Field(default=None, description="Short summary of the artifact content")


class UpdateArtifactArgs(BaseModel):
    artifact_id: str = Field(..., description="Unique UUID of the artifact to update")
    content: str = Field(..., description="New content for the artifact")
    summary: Optional[str] = Field(default=None, description="Changelog summary for this new version")


class PatchArtifactArgs(BaseModel):
    artifact_id: str = Field(..., description="Unique UUID of the artifact to change")
    search_block: str = Field(..., description="Exact existing text to replace")
    replacement_block: str = Field(..., description="New text")
    replace_all: bool = Field(default=False, description="Replace every occurrence instead of exactly one")
    summary: Optional[str] = Field(default=None, description="Changelog summary for this new version")


class ReadArtifactArgs(BaseModel):
    artifact_id: str = Field(..., description="Unique UUID of the artifact to read")


class LaunchAppArgs(BaseModel):
    name_or_path: str = Field(..., description="Name of application alias or executable path")


class FocusAppArgs(BaseModel):
    name_or_title_substring: str = Field(..., description="Substring of window title or process name to focus")


class SetVolumeArgs(BaseModel):
    level: Optional[int] = Field(default=None, ge=0, le=100, description="Absolute volume, 0-100")
    change: Optional[int] = Field(default=None, ge=-100, le=100, description="Relative step, e.g. 10 or -5")


class SetMuteArgs(BaseModel):
    on: bool = Field(..., description="True to mute, False to unmute")


class MediaControlArgs(BaseModel):
    action: Literal["status", "play", "pause", "play_pause", "next", "previous", "stop"] = Field(
        default="status", description="status = what is playing; the rest control playback")


class GetClipboardArgs(BaseModel):
    pass


class SetClipboardArgs(BaseModel):
    text: str = Field(..., description="Text content to copy to clipboard")


class ListProcessesArgs(BaseModel):
    filter_name: Optional[str] = Field(default=None, description="Optional process name filter substring")
    sort_by: Literal["memory", "cpu"] = Field(default="memory", description="Order by memory use, or by CPU use")


class KillProcessArgs(BaseModel):
    pid_or_name: str = Field(..., description="Process PID or process name to close")
    force: bool = Field(default=False, description="End it immediately, losing unsaved work -- only when the user says force or it is frozen")


class RemindMeArgs(BaseModel):
    text: str = Field(..., description="What to remind the user of, phrased to be read out")
    when: str = Field(..., description="'in 20 minutes', '17:30', '5pm', 'tomorrow 9am' or '2026-10-02 08:00' (local time)")
    repeat: Literal["once", "daily", "weekdays", "weekly"] = Field(default="once", description="How often")


class RemindersArgs(BaseModel):
    action: Literal["list", "cancel"] = Field(default="list", description="List reminders, or cancel one")
    reminder_id: str = Field(default="", description="For cancel: the reminder's id from the list")


class GetSystemStatusArgs(BaseModel):
    pass


class GitStatusArgs(BaseModel):
    repo_path: str = Field(default=".", description="Repository directory relative to the workspace")


class GitDiffArgs(BaseModel):
    file_path: Optional[str] = Field(default=None, description="Limit the diff to this file")
    staged: bool = Field(default=False, description="True for changes staged for commit, False for unstaged changes")
    repo_path: str = Field(default=".", description="Repository directory relative to the workspace")


class GitLogArgs(BaseModel):
    limit: int = Field(default=10, description="How many commits to show (1-100)")
    file_path: Optional[str] = Field(default=None, description="Show the history of this file only")
    repo_path: str = Field(default=".", description="Repository directory relative to the workspace")


class GitCommitArgs(BaseModel):
    message: str = Field(..., description="The commit message")
    files: Optional[list[str]] = Field(default=None, description="Only these paths (relative to the repository) are committed; omit to commit every change")
    repo_path: str = Field(default=".", description="Repository directory relative to the workspace")


class GitSwitchArgs(BaseModel):
    branch: str = Field(..., description="Branch to switch to")
    create: bool = Field(default=False, description="Create the branch first (from the current commit)")
    repo_path: str = Field(default=".", description="Repository directory relative to the workspace")


class GitRestoreArgs(BaseModel):
    file_path: str = Field(..., description="File whose uncommitted changes are thrown away, relative to the repository")
    repo_path: str = Field(default=".", description="Repository directory relative to the workspace")


class GitRemoteArgs(BaseModel):
    repo_path: str = Field(default=".", description="Repository directory relative to the workspace")


class RememberArgs(BaseModel):
    content: str = Field(..., description="One fact, stated plainly so it makes sense on its own later (under 300 characters)")
    kind: Literal[tuple(MEMORY_KINDS)] = Field(  # type: ignore[valid-type]
        default="project",
        description=(
            "about_user: who the user is. preference: how they like things. workflow: how to work "
            "with them. project: facts/conventions of this project. decision: what was settled "
            "and why. task: open work to pick up later. The first three follow the user everywhere; "
            "the rest belong to this workspace."
        ),
    )
    replaces: str = Field(default="", description="Id of an existing memory this corrects or updates, from the memory list; empty for a new one")


class ForgetArgs(BaseModel):
    memory_id: str = Field(..., description="Id of the memory to delete, as shown in the memory list")


TOOL_SCHEMAS: dict[str, type[BaseModel]] = {
    "read_file": ReadFileArgs,
    "list_directory": ListDirectoryArgs,
    "execute_command": ExecuteCommandArgs,
    "delete_file": DeleteFileArgs,
    "web_search": WebSearchArgs,
    "fetch_url": FetchUrlArgs,
    "write_file": WriteFileArgs,
    "patch_file": PatchFileArgs,
    "find_files": FindFilesArgs,
    "grep_in_files": GrepInFilesArgs,
    # Artifact Tools
    "create_artifact": CreateArtifactArgs,
    "update_artifact": UpdateArtifactArgs,
    "patch_artifact": PatchArtifactArgs,
    "read_artifact": ReadArtifactArgs,
    # Phase 3 OS Tools
    "launch_app": LaunchAppArgs,
    "focus_app": FocusAppArgs,
    "set_volume": SetVolumeArgs,
    "set_mute": SetMuteArgs,
    "media_control": MediaControlArgs,
    "get_clipboard": GetClipboardArgs,
    "set_clipboard": SetClipboardArgs,
    "list_processes": ListProcessesArgs,
    "kill_process": KillProcessArgs,
    "remind_me": RemindMeArgs,
    "reminders": RemindersArgs,
    "get_system_status": GetSystemStatusArgs,
    "git_status": GitStatusArgs,
    "git_diff": GitDiffArgs,
    "git_log": GitLogArgs,
    "git_commit": GitCommitArgs,
    "git_switch": GitSwitchArgs,
    "git_restore": GitRestoreArgs,
    "git_push": GitRemoteArgs,
    "git_pull": GitRemoteArgs,
    "remember": RememberArgs,
    "forget": ForgetArgs,
}


def get_tool_schema(tool_name: str) -> Optional[type[BaseModel]]:
    """Retrieve Pydantic validation schema for a registered tool."""
    return TOOL_SCHEMAS.get(tool_name)


RUNTIME_ONLY_PARAMS = frozenset({"user_message", "allow_outside"})


def execute_tool(
    tool_name: str,
    arguments: dict[str, Any],
    context: Optional[dict[str, Any]] = None
) -> str:
    """
    Execute a registered tool by name with provided arguments, automatically
    filtering any extra hallucinated keyword arguments from smaller LLMs and
    injecting runtime execution context (workspace_path, project_id, session_id).
    """
    if tool_name not in TOOL_FUNCTIONS:
        logger.warning("Attempted to execute unregistered tool: '%s'", tool_name)
        return f"Error: Tool '{tool_name}' is not registered."
    
    if context and str(context.get("chat_mode") or "").upper() == FREEFORM_MODE and tool_name in WORKSPACE_BOUND_TOOLS:
        return (
            f"Error: '{tool_name}' works on project files, and this is a Freeform conversation "
            "with no workspace. Ask the user to switch to a Workspace for that."
        )

    func = TOOL_FUNCTIONS[tool_name]
    try:
        # Filter arguments based on function signature
        sig = inspect.signature(func)
        valid_params = set(sig.parameters.keys())
        # Runtime context, never model arguments (see the injection below).
        filtered_args = {k: v for k, v in arguments.items() if k in valid_params and k not in RUNTIME_ONLY_PARAMS}

        # Context injection for workspace-aware and session-aware tools
        if context:
            if "workspace_path" in valid_params and "workspace_path" not in filtered_args and "workspace_path" in context:
                filtered_args["workspace_path"] = context["workspace_path"]
            if "project_id" in valid_params and "project_id" not in filtered_args and "project_id" in context:
                filtered_args["project_id"] = context["project_id"]
            if "session_id" in valid_params and "session_id" not in filtered_args and "session_id" in context:
                filtered_args["session_id"] = context["session_id"]
            if "user_message" in valid_params and "user_message" in context:
                # Always the turn's real message: a model-supplied value must not stand in for it.
                filtered_args["user_message"] = context["user_message"]
            if "allow_outside" in valid_params and context.get("permission_checked"):
                # The tool loops set this after the permission gate passed the call -- which asks
                # the user for any path outside the workspace -- so the tool may follow it there.
                filtered_args["allow_outside"] = True
            if "context" in valid_params and "context" not in filtered_args:
                filtered_args["context"] = context

        logger.info("Executing tool '%s' with filtered args: %s", tool_name, filtered_args)
        result = func(**filtered_args)
        return str(result)
    except TypeError as te:
        logger.error("Argument error calling '%s': %s", tool_name, te)
        return f"Error invalid arguments for '{tool_name}': {str(te)}"
    except Exception as e:
        logger.error("Unhandled error in tool '%s': %s", tool_name, e)
        return f"Error executing tool '{tool_name}': {str(e)}"


# A Freeform conversation has no project and no working directory, so nothing that reads,
# writes or runs inside a workspace is offered there -- and execute_tool refuses it even if the
# model calls one anyway (a model can name a tool it was never offered).
FREEFORM_MODE = "FREEFORM"
WORKSPACE_BOUND_TOOLS = frozenset({
    "read_file", "write_file", "patch_file", "delete_file", "find_files", "grep_in_files",
    "list_directory", "execute_command", "create_artifact", "update_artifact", "patch_artifact", "read_artifact",
    "git_status", "git_diff", "git_log", "git_commit", "git_switch",
    "git_restore", "git_push", "git_pull",
})


def tools_for_space(chat_mode: str = "WORKSPACE") -> list[Callable[..., Any]]:
    """
    The tools offered on every turn in a space -- the same list, in the same order, from the
    first turn on. The chat template renders the schemas into the head of the prompt, and
    llama-server reuses its KV cache only for a byte-identical prefix, so a set that grew with
    each message's keywords re-prefilled the conversation whenever it changed (and a keyword
    miss left the model with no way to act: "delete secret.txt" offered nothing, and it said
    "deleted" anyway). Freeform gets everything that isn't bound to a workspace (~2k tokens);
    a workspace gets all of them (~4k). Agreed in the tool review, 2026-09-30.
    """
    if (chat_mode or "").upper() == FREEFORM_MODE:
        return [t for t in AVAILABLE_TOOLS if t.__name__ not in WORKSPACE_BOUND_TOOLS]
    return list(AVAILABLE_TOOLS)
