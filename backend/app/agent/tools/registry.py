import inspect
import logging
import re
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
from app.agent.tools.artifacts import create_artifact, update_artifact, read_artifact
from app.agent.tools.system_status import get_system_status
from app.agent.tools.git import git_status, git_diff, git_log, git_commit, git_checkout
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
    "get_system_status": get_system_status,
    # Git (ported from the never-offered app/tools BaseTool stack)
    "git_status": git_status,
    "git_diff": git_diff,
    "git_log": git_log,
    "git_commit": git_commit,
    "git_checkout": git_checkout,
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
    # No play_audio/stop_playback: Jarvis's speech is played by the client, so the backend has
    # no audio of its own for a model to start or stop (removed 2026-09-26).
    get_system_status,
    # Git
    git_status,
    git_diff,
    git_log,
    git_commit,
    git_checkout,
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


class GitCheckoutArgs(BaseModel):
    target: str = Field(..., description="Branch to switch to, or file to restore to its last committed state")
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
    "get_system_status": GetSystemStatusArgs,
    "git_status": GitStatusArgs,
    "git_diff": GitDiffArgs,
    "git_log": GitLogArgs,
    "git_commit": GitCommitArgs,
    "git_checkout": GitCheckoutArgs,
    "remember": RememberArgs,
    "forget": ForgetArgs,
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


_SYSTEM_STATUS_RE = re.compile(
    r"\b(?:system (?:usage|status|state|load|resources?)|resource usage|usage|performance|"
    r"vram|gpu|graphics card|cpu|ram|memory|temperature|temps?|how hot|disk space|storage|"
    r"battery|loaded model|which model|throttl\w*|hardware|"
    r"how(?:'s| is) (?:the |my )?(?:machine|system|pc|laptop|computer))\b"
)


_GIT_RE = re.compile(
    r"\b(?:git|commits?|committed|uncommitted|branch(?:es)?|checkout|diff|staged|unstaged|repo|"
    r"repository|commit history|what (?:did i|have i|i) changed?|changes since)\b"
)


# A Freeform conversation has no project and no working directory, so nothing that reads,
# writes or runs inside a workspace is offered there -- and execute_tool refuses it even if the
# model calls one anyway (a model can name a tool it was never offered).
FREEFORM_MODE = "FREEFORM"
WORKSPACE_BOUND_TOOLS = frozenset({
    "read_file", "write_file", "patch_file", "delete_file", "find_files", "grep_in_files",
    "list_directory", "execute_command", "create_artifact", "update_artifact", "read_artifact",
    "git_status", "git_diff", "git_log", "git_commit", "git_checkout",
})


def get_relevant_tools(
    query: str,
    chat_mode: str = "WORKSPACE",
    matched_skills: Optional[list] = None
) -> list[Callable[..., Any]]:
    """
    Intelligently filters tool schemas to reduce prompt prefill token bloat.
    Returns the minimal subset of relevant tools based on query intent.
    """
    tools = _select_tools(query, chat_mode, matched_skills)
    if (chat_mode or "").upper() == FREEFORM_MODE:
        tools = [t for t in tools if t.__name__ not in WORKSPACE_BOUND_TOOLS]
    # Memory is offered on every turn: what is worth keeping is the model's call, and it can come
    # up in any message ("I'm on the night shift this week", "let's go with SQLite"), not only
    # after a trigger word. Always the same two, so the offered set -- and the prompt prefix --
    # stays stable.
    tools = list(tools) + [t for t in MEMORY_TOOLS if t not in tools]
    return tools


MEMORY_TOOLS = (remember, forget)


def _select_tools(
    query: str,
    chat_mode: str = "WORKSPACE",
    matched_skills: Optional[list] = None
) -> list[Callable[..., Any]]:
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

    # 5. Git -- "what did I change", "commit this", "which branch". Word boundaries: "branch" is
    # safe, but a bare "log" would match "login"/"catalog", so history needs "git log"/"history".
    if _GIT_RE.search(q):
        tools.update((git_status, git_diff, git_log, git_commit, git_checkout))

    # 6. Machine state -- the prompt no longer carries a hardware line, so a deliberate question
    # about the machine has to be able to measure it. Word boundaries: "ram" is in "program".
    if _SYSTEM_STATUS_RE.search(q):
        tools.add(get_system_status)

    if tools:
        return list(tools)

    action_words = ("do", "check", "fix", "inspect", "show", "list", "diagnose", "review", "test", "build", "generate", "update")
    if chat_mode == "SYSTEM" or any(w in q for w in action_words):
        return AVAILABLE_TOOLS

    return []
