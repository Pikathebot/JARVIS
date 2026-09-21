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
from app.agent.tools.media_control import set_volume, mute_toggle, media_key
from app.agent.tools.clipboard_control import get_clipboard, set_clipboard
from app.agent.tools.process_control import list_processes, kill_process
from app.agent.tools.notify import send_toast
from app.agent.tools.audio_playback import play_audio, stop_playback
from app.agent.tools.artifacts import create_artifact, update_artifact, read_artifact

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
    "read_artifact": read_artifact,
    # Phase 3 OS Tools
    "launch_app": launch_app,
    "focus_app": focus_app,
    "set_volume": set_volume,
    "mute_toggle": mute_toggle,
    "media_key": media_key,
    "get_clipboard": get_clipboard,
    "set_clipboard": set_clipboard,
    "list_processes": list_processes,
    "kill_process": kill_process,
    "send_toast": send_toast,
    "play_audio": play_audio,
    "stop_playback": stop_playback,
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
    read_artifact,
    # Phase 3 OS Tools
    launch_app,
    focus_app,
    set_volume,
    mute_toggle,
    media_key,
    get_clipboard,
    set_clipboard,
    list_processes,
    kill_process,
    send_toast,
    # play_audio is deliberately absent: it takes raw audio bytes, which no model can supply
    # (a string from a model just raises). It stays in TOOL_FUNCTIONS for the backend's own use.
    stop_playback,
]


class ReadFileArgs(BaseModel):
    file_path: str = Field(..., description="Path to the file to read")


class ListDirectoryArgs(BaseModel):
    directory_path: str = Field(default=".", description="Path of directory to list")


class ExecuteCommandArgs(BaseModel):
    command: str = Field(..., description="Shell command string to execute")


class DeleteFileArgs(BaseModel):
    file_path: str = Field(..., description="Path of file to delete")


class WebSearchArgs(BaseModel):
    query: str = Field(..., description="Search query string")
    max_results: int = Field(default=5, ge=1, le=10, description="Max results")


class FetchUrlArgs(BaseModel):
    url: str = Field(..., description="Web URL to fetch")
    max_chars: int = Field(default=8000, ge=500, le=25000, description="Max character budget")


class WriteFileArgs(BaseModel):
    file_path: str = Field(..., description="Target file path")
    content: str = Field(..., description="Content to write")
    overwrite: bool = Field(default=True, description="Overwrite if exists")


class PatchFileArgs(BaseModel):
    file_path: str = Field(..., description="Target file path")
    search_block: str = Field(..., description="Exact code block to replace")
    replacement_block: str = Field(..., description="New code block")


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


class ReadArtifactArgs(BaseModel):
    artifact_id: str = Field(..., description="Unique UUID of the artifact to read")


class LaunchAppArgs(BaseModel):
    name_or_path: str = Field(..., description="Name of application alias or executable path")


class FocusAppArgs(BaseModel):
    name_or_title_substring: str = Field(..., description="Substring of window title or process name to focus")


class SetVolumeArgs(BaseModel):
    level: int = Field(..., ge=0, le=100, description="Volume level from 0 to 100")


class MuteToggleArgs(BaseModel):
    pass


class MediaKeyArgs(BaseModel):
    action: Literal["play_pause", "next", "previous", "stop"] = Field(..., description="Media playback action")


class GetClipboardArgs(BaseModel):
    pass


class SetClipboardArgs(BaseModel):
    text: str = Field(..., description="Text content to copy to clipboard")


class ListProcessesArgs(BaseModel):
    filter_name: Optional[str] = Field(default=None, description="Optional process name filter substring")


class KillProcessArgs(BaseModel):
    pid_or_name: str = Field(..., description="Process PID or process name to terminate")


class SendToastArgs(BaseModel):
    title: str = Field(..., description="Toast notification header title")
    message: str = Field(..., description="Toast notification message body")
    urgent: bool = Field(default=False, description="Flag for urgent/high priority toast")


class PlayAudioArgs(BaseModel):
    audio_bytes: bytes = Field(..., description="Raw audio bytes to play")


class StopPlaybackArgs(BaseModel):
    pass


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
    "read_artifact": ReadArtifactArgs,
    # Phase 3 OS Tools
    "launch_app": LaunchAppArgs,
    "focus_app": FocusAppArgs,
    "set_volume": SetVolumeArgs,
    "mute_toggle": MuteToggleArgs,
    "media_key": MediaKeyArgs,
    "get_clipboard": GetClipboardArgs,
    "set_clipboard": SetClipboardArgs,
    "list_processes": ListProcessesArgs,
    "kill_process": KillProcessArgs,
    "send_toast": SendToastArgs,
    "play_audio": PlayAudioArgs,
    "stop_playback": StopPlaybackArgs,
}


def get_tool_schema(tool_name: str) -> Optional[type[BaseModel]]:
    """Retrieve Pydantic validation schema for a registered tool."""
    return TOOL_SCHEMAS.get(tool_name)


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
    
    func = TOOL_FUNCTIONS[tool_name]
    try:
        # Filter arguments based on function signature
        sig = inspect.signature(func)
        valid_params = set(sig.parameters.keys())
        filtered_args = {k: v for k, v in arguments.items() if k in valid_params}

        # Context injection for workspace-aware and session-aware tools
        if context:
            if "workspace_path" in valid_params and "workspace_path" not in filtered_args and "workspace_path" in context:
                filtered_args["workspace_path"] = context["workspace_path"]
            if "project_id" in valid_params and "project_id" not in filtered_args and "project_id" in context:
                filtered_args["project_id"] = context["project_id"]
            if "session_id" in valid_params and "session_id" not in filtered_args and "session_id" in context:
                filtered_args["session_id"] = context["session_id"]
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


def get_relevant_tools(
    query: str,
    chat_mode: str = "WORKSPACE",
    matched_skills: Optional[list] = None
) -> list[Callable[..., Any]]:
    """
    Intelligently filters tool schemas to reduce prompt prefill token bloat.
    Returns the minimal subset of relevant tools based on query intent.
    """
    if matched_skills and len(matched_skills) > 0:
        return AVAILABLE_TOOLS

    q = (query or "").lower().strip()

    # Conversational / chitchat / direct conceptual queries need NO tools
    chitchat_triggers = {
        "hi", "hello", "hey", "sup", "greetings", "good morning", "good evening",
        "who are you", "what are you", "how are you", "help", "thanks", "thank you"
    }
    if q in chitchat_triggers or len(q) < 4:
        if not any(w in q for w in ("file", "find", "search", "open", "run", "read", "write", "artifact")):
            return []

    tools = set()

    # 1. Web search triggers
    if any(w in q for w in ("search", "google", "look up", "online", "internet", "website", "url", "http://", "https://", "latest news", "weather", "who won", "what is the price", "documentation")):
        tools.add(web_search)
        tools.add(fetch_url)

    # 2. File & Code triggers
    file_triggers = (
        "file", "read", "write", "patch", "edit", "modify", "create", "delete",
        "directory", "folder", "dir", "code", "grep", "find", "script", "content",
        ".py", ".js", ".ts", ".html", ".css", ".json", ".md", ".txt", ".sh", ".bat", ".ps1"
    )
    if any(w in q for w in file_triggers):
        tools.add(read_file)
        tools.add(write_file)
        tools.add(patch_file)
        tools.add(find_files)
        tools.add(grep_in_files)
        tools.add(list_directory)
        # The permission gate makes this a confirmation, not the selection. Leaving it out
        # meant "delete secret.txt" reached the model with no way to delete anything -- and
        # it answered "deleted" anyway.
        tools.add(delete_file)

    # 3. Artifact triggers
    artifact_triggers = (
        "artifact", "deliverable", "document", "report", "save code", "create artifact",
        "generate artifact", "update artifact", "read artifact", "markdown document"
    )
    if any(w in q for w in artifact_triggers):
        tools.add(create_artifact)
        tools.add(update_artifact)
        tools.add(read_artifact)

    # 4. System / App / OS triggers
    os_triggers = (
        "open", "launch", "app", "window", "volume", "sound", "mute", "unmute",
        "music", "play", "pause", "clipboard", "copy", "paste", "process",
        "task", "kill", "terminate", "notification", "toast", "powershell",
        "command", "terminal", "run"
    )
    if any(w in q for w in os_triggers):
        tools.add(launch_app)
        tools.add(focus_app)
        tools.add(set_volume)
        tools.add(mute_toggle)
        tools.add(media_key)
        tools.add(get_clipboard)
        tools.add(set_clipboard)
        tools.add(list_processes)
        tools.add(kill_process)
        tools.add(send_toast)
        tools.add(execute_command)

    if tools:
        return list(tools)

    action_words = ("do", "check", "fix", "inspect", "show", "list", "diagnose", "review", "test", "build", "generate", "update")
    if chat_mode == "SYSTEM" or any(w in q for w in action_words):
        return AVAILABLE_TOOLS

    return []
