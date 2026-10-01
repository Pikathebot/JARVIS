import hashlib
import ipaddress
import json
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import urlparse
import psutil


CONFIRMATION_TIMEOUT_ACTION: Literal["deny", "allow"] = "deny"

# Per-tool rate limits per turn (tool_name -> max invocations per turn)
# Default unset = no per-tool limit beyond MAX_TOOL_CALLS_PER_TURN
RATE_LIMITS: dict[str, int] = {}


def check_rate_limit(tool_name: str, current_turn_tool_count: int) -> bool:
    """
    Check per-tool rate limiting independently of global turn caps.
    Returns True if permitted, False if limit exceeded.
    """
    limit = RATE_LIMITS.get(tool_name)
    if limit is not None and current_turn_tool_count >= limit:
        return False
    return True


class ChatMode(str, Enum):
    WORKSPACE = "WORKSPACE"
    SYSTEM = "SYSTEM"


class RiskTier(str, Enum):
    LOW_RISK = "LOW_RISK"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    HIGH_RISK = "HIGH_RISK"




# Base hardcoded lookup table mapping tool name -> default RiskTier.
# Lookups take O(1) time.
BASE_TOOL_RISK_MAP: dict[str, RiskTier] = {
    "read_file": RiskTier.LOW_RISK,
    "list_directory": RiskTier.LOW_RISK,
    "find_files": RiskTier.LOW_RISK,
    "grep_in_files": RiskTier.LOW_RISK,
    "write_file": RiskTier.LOW_RISK,
    "patch_file": RiskTier.LOW_RISK,
    "web_search": RiskTier.LOW_RISK,
    "fetch_url": RiskTier.LOW_RISK,
    "execute_command": RiskTier.CONFIRMATION_REQUIRED,
    "delete_file": RiskTier.HIGH_RISK,
    # Artifact Tools (Build Plan §15)
    "create_artifact": RiskTier.LOW_RISK,
    "update_artifact": RiskTier.LOW_RISK,
    "read_artifact": RiskTier.LOW_RISK,
    # Phase 3: Windows OS Power Controls & Desktop Toast Alerts
    "launch_app": RiskTier.CONFIRMATION_REQUIRED,
    "focus_app": RiskTier.LOW_RISK,
    "set_volume": RiskTier.LOW_RISK,
    "mute_toggle": RiskTier.LOW_RISK,
    "media_key": RiskTier.LOW_RISK,
    # Reading runs when the user's message points at the clipboard (see _CLIPBOARD_CUE_RE);
    # writing it is no riskier than the user pressing Ctrl+C (tool review, 2026-09-30).
    "get_clipboard": RiskTier.CONFIRMATION_REQUIRED,
    "set_clipboard": RiskTier.LOW_RISK,
    "list_processes": RiskTier.LOW_RISK,
    # Killing a program loses its unsaved work, so it always asks; the reason says which kind.
    "kill_process": RiskTier.CONFIRMATION_REQUIRED,
    "send_toast": RiskTier.LOW_RISK,
    "get_system_status": RiskTier.LOW_RISK,
    # Git: reads are free; commit and checkout rewrite the working tree / history.
    "git_status": RiskTier.LOW_RISK,
    "git_diff": RiskTier.LOW_RISK,
    "git_log": RiskTier.LOW_RISK,
    "git_commit": RiskTier.CONFIRMATION_REQUIRED,
    "git_checkout": RiskTier.CONFIRMATION_REQUIRED,
    # Memory: the model's own notes, shown to the user as a tool card; ephemeral turns refuse.
    "remember": RiskTier.LOW_RISK,
    "forget": RiskTier.LOW_RISK,
}


# Windows core/critical system processes that must never be terminated without explicit user confirmation.
# Named constant defined per Phase 3 spec §3.4 — extendable and auditable.
MAJOR_PROCESS_NAMES: set[str] = {
    "explorer.exe",
    "svchost.exe",
    "winlogon.exe",
    "csrss.exe",
    "wininit.exe",
    "services.exe",
    "lsass.exe",
    "dwm.exe",
    "system",
    "registry",
    "smss.exe",
    "spoolsv.exe",
}



# Read-only commands that run without asking -- but only as one plain command (see
# ``evaluate_command_argument_risk``). Matched on whole leading words, so "dir" is not "dirty.exe".
SAFE_COMMAND_PREFIXES = (
    "git status",
    "git log",
    "git branch",
    "git diff",
    "dir",
    "ls",
    "echo",
    "pwd",
    "whoami",
    "cat",
    "type",
    "head",
    "tail",
    "more",
    "get-content",
    "findstr",
    "python --version",
    "pytest --version",
)

# Safe commands whose arguments are text, not paths, so they need no workspace check.
_SAFE_WITHOUT_PATHS = {"echo", "pwd", "whoami", "python --version", "pytest --version"}

# Anything that makes one command several, redirects output into a file, or splices in another
# command's output or a variable: `echo hi; Remove-Item ...`, `dir && del x`, `echo "" > a.md`,
# `echo $(Stop-Computer)`, `echo $env:SECRET`. A safe command containing any of these is no
# longer the command that was vetted.
_SHELL_COMPOSITION = re.compile(r"[;&|<>`$\r\n]|@\(")

# Arguments of a safe command that would make it write or destroy: `git diff --output=f`,
# `git branch -D main` / `-m` / `-c` / `--delete` / `--move` / `--copy` / `-f`.
_UNSAFE_SAFE_COMMAND_ARGS = re.compile(
    r"(?:^|\s)(?:--output\b|-[a-z]*[dDmMcCf][a-z]*\b|--(?:delete|move|copy|force|set-upstream-to|unset-upstream|edit-description)\b)"
)

DANGEROUS_COMMAND_PATTERNS = re.compile(
    r"\b(?:rm\s+-rf|del\s+/[sfq]|erase\s+/[sfq]|rd\s+/s|rmdir\s+/s|format(?:\.com)?\s+[a-z]:|"
    r"diskpart|shutdown|mkfs|bcdedit|vssadmin\s+delete|cipher\s+/w|reg\s+delete|"
    r"remove-item\b[^\n]*-r(?:ecurse)?\b|format-volume|clear-disk|initialize-disk|remove-partition|"
    r"stop-computer|restart-computer)",
    re.IGNORECASE
)

SYSTEM_CRITICAL_DIRECTORIES = (
    r"c:/windows",
    r"c:/program files",
    r"c:/program files (x86)",
    r"c:/system32",
    "/etc",
    "/bin",
    "/usr/bin",
    "/sbin"
)


def normalize_arguments(args: dict[str, Any]) -> dict[str, Any]:
    """
    Normalize argument keys and path strings to produce stable, canonical hashes.
    """
    normalized = {}
    for k, v in sorted(args.items()):
        if k == "reason":
            # Display-only (shown on the card). The approved turn re-issues the call, and a
            # reworded reason must still match the approval.
            continue
        canonical_key = "file_path" if k in ("path", "filePath", "filename", "file") else (
            "command" if k in ("cmd", "command_line", "cli") else (
                "directory_path" if k in ("dir", "dir_path", "directory") else k
            )
        )
        if isinstance(v, str):
            cleaned = v.strip().lstrip("./\\").replace("\\", "/")
            normalized[canonical_key] = cleaned
        else:
            normalized[canonical_key] = v
    return normalized


def generate_action_id(tool_name: str, arguments: dict[str, Any]) -> str:
    """
    Generate a deterministic, tamper-evident action ID hash for approval gating.
    """
    normalized = normalize_arguments(arguments)
    serialized = json.dumps({"tool": tool_name, "args": normalized}, sort_keys=True)
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]
    return f"act_{digest}"


_COMMAND_TOKEN = re.compile(r'"[^"]*"|\'[^\']*\'|\S+')


def _is_option(token: str) -> bool:
    """`-la`, `--stat`, and cmd-style switches like `/s` or `/c:x` -- not paths."""
    return token.startswith("-") or bool(re.match(r"^/[a-z?](?::|$)", token, re.IGNORECASE))


def _stays_in_workspace(token: str, workspace_root: Path) -> bool:
    """A path argument of a safe command must resolve inside the workspace: `cat C:\\x\\secret.txt`
    would otherwise read around read_file's own gate."""
    value = token.strip("'\"")
    if not value:
        return True
    if value.startswith("~") or value.startswith("\\\\"):
        return False  # home directory / UNC share
    try:
        p = Path(value)
        resolved = p.resolve() if p.is_absolute() else (workspace_root / p).resolve()
    except Exception:
        return False
    return resolved == workspace_root or workspace_root in resolved.parents


def evaluate_command_argument_risk(command: str, workspace_root: Optional[Path | str] = None) -> RiskTier:
    """
    Argument-aware tier for execute_command. Destructive commands are HIGH_RISK. A known
    read-only command runs without asking only when it is exactly one plain command -- no
    chaining, pipes, redirection, substitution or variables -- none of its arguments turn it into
    a write, and every path it names is inside the workspace. Everything else asks.
    """
    cmd_clean = str(command or "").strip()

    if DANGEROUS_COMMAND_PATTERNS.search(cmd_clean):
        return RiskTier.HIGH_RISK
    if not cmd_clean or _SHELL_COMPOSITION.search(cmd_clean):
        return RiskTier.CONFIRMATION_REQUIRED

    tokens = _COMMAND_TOKEN.findall(cmd_clean)
    lowered = [t.lower() for t in tokens]
    for prefix in SAFE_COMMAND_PREFIXES:
        words = prefix.split()
        if lowered[:len(words)] != words:
            continue
        rest = tokens[len(words):]
        if _UNSAFE_SAFE_COMMAND_ARGS.search(" " + " ".join(rest)):
            return RiskTier.CONFIRMATION_REQUIRED
        if prefix in _SAFE_WITHOUT_PATHS:
            return RiskTier.LOW_RISK
        root = Path(workspace_root or WORKSPACE_ROOT).resolve()
        if all(_is_option(t) or _stays_in_workspace(t, root) for t in rest):
            return RiskTier.LOW_RISK
        return RiskTier.CONFIRMATION_REQUIRED

    return RiskTier.CONFIRMATION_REQUIRED


WORKSPACE_ROOT = Path(__file__).resolve().parent.parent.parent.parent


def evaluate_file_path_risk(
    file_path: str,
    is_write_or_delete: bool = False,
    chat_mode: ChatMode = ChatMode.WORKSPACE,
    workspace_root: Optional[Path | str] = None
) -> RiskTier:
    """
    Check if a file path targets protected OS system directories or lies outside the project workspace.
    Canonicalizes the path, resolves symlinks, and evaluates risk based on ChatMode (WORKSPACE vs SYSTEM).
    """
    clean_path_str = str(file_path or "").strip()
    if not clean_path_str:
        return RiskTier.LOW_RISK

    norm_path = os.path.normpath(clean_path_str).lower().replace("\\", "/")
    
    # 1. Immediate HIGH_RISK check for critical OS directories (always enforced in all modes)
    for sys_dir in SYSTEM_CRITICAL_DIRECTORIES:
        if norm_path.startswith(sys_dir):
            return RiskTier.HIGH_RISK

    # 2. Check workspace containment and canonicalization
    try:
        workspace_resolved = Path(workspace_root or WORKSPACE_ROOT).resolve()
        p = Path(clean_path_str)
        if p.is_absolute():
            resolved = p.resolve()
        else:
            resolved = (workspace_resolved / p).resolve()
        
        # If the path is inside the project workspace directory -> safe in all modes
        if resolved == workspace_resolved or workspace_resolved in resolved.parents:
            return RiskTier.LOW_RISK
        
        # 3. Path is outside workspace root:
        # In SYSTEM mode, non-critical paths are LOW_RISK
        if chat_mode == ChatMode.SYSTEM:
            return RiskTier.LOW_RISK

        # In WORKSPACE mode (or default/fallback): external path requires confirmation
        return RiskTier.HIGH_RISK if is_write_or_delete else RiskTier.CONFIRMATION_REQUIRED

    except Exception:
        return RiskTier.CONFIRMATION_REQUIRED


PRIVATE_HOST_NAMES = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "host.docker.internal"}


def evaluate_url_risk(url: str) -> RiskTier:
    """
    Check if a URL targets local/internal private IP addresses or dangerous protocols (SSRF protection).
    """
    raw_url = str(url or "").strip()
    if not raw_url:
        return RiskTier.LOW_RISK

    try:
        parsed = urlparse(raw_url if "://" in raw_url else f"https://{raw_url}")
        scheme = parsed.scheme.lower()
        if scheme not in ("http", "https"):
            return RiskTier.HIGH_RISK

        hostname = (parsed.hostname or "").strip().lower()
        if not hostname:
            return RiskTier.CONFIRMATION_REQUIRED

        if hostname in PRIVATE_HOST_NAMES or hostname.endswith(".local") or hostname.endswith(".internal"):
            return RiskTier.CONFIRMATION_REQUIRED

        try:
            ip = ipaddress.ip_address(hostname)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified:
                return RiskTier.CONFIRMATION_REQUIRED
        except ValueError:
            # Domain name, not an IP literal
            pass

        return RiskTier.LOW_RISK
    except Exception:
        return RiskTier.CONFIRMATION_REQUIRED


# Where a URL the model fetches may come from without asking: the user's own messages and what
# web_search / fetch_url returned this session. A URL the model composed itself can carry chat or
# file content out in its path or query (`https://x.example/?q=<document text>`) -- the way a
# prompt injection on a fetched page would exfiltrate data -- so that one asks first.
URL_SOURCE_TOOLS = ("web_search", "fetch_url")


def _url_key(url: str) -> str:
    """A URL as it would be written in prose: no scheme, no www., no fragment or trailing slash."""
    key = str(url or "").strip().lower()
    key = re.sub(r"^[a-z][a-z0-9+.\-]*://", "", key)
    key = key.split("#", 1)[0].rstrip("/")
    return key[4:] if key.startswith("www.") else key


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # multimodal user turn: keep the text parts
        return " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return ""


def url_provenance_text(messages: list[dict[str, Any]]) -> str:
    """The text a fetched URL must appear in to run without asking (see URL_SOURCE_TOOLS)."""
    parts = []
    for m in messages or []:
        role = m.get("role")
        if role == "user" or (role == "tool" and m.get("name") in URL_SOURCE_TOOLS):
            parts.append(_message_text(m))
    text = "\n".join(parts).lower()
    return re.sub(r"[a-z][a-z0-9+.\-]*://(?:www\.)?", "", text)


# "what's on my clipboard", "summarize what I copied", "paste it here" -- the user pointing at the
# clipboard. Without that, a model reaching for it unprompted asks first.
_CLIPBOARD_CUE_RE = re.compile(r"\b(?:clipboard|copied|paste[ds]?|pasting)\b", re.IGNORECASE)


def url_came_from_conversation(url: str, provenance_text: str) -> bool:
    key = _url_key(url)
    return bool(key) and key in provenance_text


def evaluate_launch_app_risk(name_or_path: str) -> tuple[RiskTier, Optional[str]]:
    """
    Opening an installed application by name ("discord", "settings", "notepad") is what the
    user asked for and carries no more risk than clicking its Start Menu tile, so it is
    LOW_RISK. Confirmation stays for anything that is really "run this file": an explicit path,
    a script (.cmd/.bat/.ps1/.vbs), or an executable under Downloads or a temp folder.
    """
    from app.agent.tools.app_control import APP_ALIASES, URI_APPS, resolve_executable, is_uri_or_shortcut

    raw = (name_or_path or "").strip().strip("'\"")
    lowered = raw.lower()
    if not raw:
        return RiskTier.CONFIRMATION_REQUIRED, None
    if lowered in APP_ALIASES or lowered in URI_APPS:
        return RiskTier.LOW_RISK, "Known application alias."
    if any(sep in raw for sep in ("\\", "/")) or lowered.endswith((".cmd", ".bat", ".ps1", ".vbs", ".js", ".msi")):
        return RiskTier.CONFIRMATION_REQUIRED, "Launching an explicit path or script needs confirmation."

    try:
        target = resolve_executable(raw)
    except Exception:
        target = None
    if not target:
        # Unknown app: launching will fail with a clear error, so nothing to confirm.
        return RiskTier.LOW_RISK, "Application not found; launch will report that."
    target_l = target.lower()
    if is_uri_or_shortcut(target):
        return RiskTier.LOW_RISK, "Installed application opened via the Start Menu or a system URI."
    # Raw strings: as plain literals "\temp\\" held a tab and "\appdata" a bell, so this never matched.
    if any(marker in target_l for marker in (r"\downloads" "\\", r"\temp" "\\", r"\tmp" "\\")):
        return RiskTier.CONFIRMATION_REQUIRED, "Executable lives in a downloads or temp folder."
    if target_l.endswith((".cmd", ".bat", ".ps1", ".vbs")):
        return RiskTier.CONFIRMATION_REQUIRED, "Target is a script."
    return RiskTier.LOW_RISK, "Installed application on PATH."


def evaluate_kill_process_risk(pid_or_name: Any) -> tuple[RiskTier, Optional[str]]:
    """
    Evaluate risk tier for kill_process:
    1. Target matching Jarvis's own backend process (PID or self-process) is flagged for self-protection.
    2. Target process in MAJOR_PROCESS_NAMES -> CONFIRMATION_REQUIRED.
    3. Process name matching > 1 running instances -> CONFIRMATION_REQUIRED (multi-instance safety stop).
    4. Anything else still asks: a killed program loses its unsaved work, which can't be undone.
    Always CONFIRMATION_REQUIRED; the tier stays in the return so the reason can name the case.
    """
    target_str = str(pid_or_name or "").strip()
    if not target_str:
        return RiskTier.LOW_RISK, "Empty process identifier."

    current_pid = os.getpid()
    parent_pid = os.getppid() if hasattr(os, "getppid") else None

    # 1. Numeric PID check
    if target_str.isdigit() or (target_str.startswith("-") and target_str[1:].isdigit()):
        try:
            pid_num = int(target_str)
            if pid_num in (current_pid, parent_pid):
                return RiskTier.CONFIRMATION_REQUIRED, "Target PID is Jarvis's own backend/launcher process (self-protection)."

            try:
                proc = psutil.Process(pid_num)
                proc_name = proc.name().lower()
                name_clean = proc_name[:-4] if proc_name.endswith(".exe") else proc_name
                if proc_name in MAJOR_PROCESS_NAMES or f"{name_clean}.exe" in MAJOR_PROCESS_NAMES:
                    return RiskTier.CONFIRMATION_REQUIRED, f"Target process '{proc_name}' (PID {pid_num}) is a Windows core/critical system process."
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            return RiskTier.CONFIRMATION_REQUIRED, f"Closes PID {pid_num}."
        except Exception:
            return RiskTier.CONFIRMATION_REQUIRED, "PID lookup failed."

    # 2. String process name check
    raw_name = target_str.lower()
    name_clean = raw_name[:-4] if raw_name.endswith(".exe") else raw_name
    name_exe = f"{name_clean}.exe"

    if raw_name in MAJOR_PROCESS_NAMES or name_exe in MAJOR_PROCESS_NAMES:
        return RiskTier.CONFIRMATION_REQUIRED, f"Target process '{target_str}' is a Windows core/critical system process."

    # Count matching running process instances
    match_count = 0
    try:
        for p in psutil.process_iter(['pid', 'name']):
            try:
                p_name = (p.info.get('name') or "").lower()
                if p_name in (raw_name, name_exe):
                    match_count += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:
        pass

    if match_count > 1:
        return RiskTier.CONFIRMATION_REQUIRED, f"Process name '{target_str}' matches {match_count} running instances. Multi-instance termination requires user confirmation."

    return RiskTier.CONFIRMATION_REQUIRED, f"Closes '{target_str}'."



@dataclass
class PermissionDecision:
    tool: str
    args: dict[str, Any]
    risk_tier: RiskTier
    allowed: bool
    action_id: str
    reason: str


def _describe_pending_action(pending: dict[str, Any]) -> str:
    """A short, speakable description of one pending action, e.g. 'run a command: git push'."""
    tool = str(pending.get("tool", "")).replace("_", " ").strip() or "an action"
    args = pending.get("args") or {}
    for key in ("command", "cmd", "file_path", "path", "url", "pid_or_name", "process_name", "name", "message", "target"):
        value = args.get(key) if isinstance(args, dict) else None
        if value:
            return f"{tool}: {value}"
    return tool


def build_confirmation_prompt(
    pending: list[dict[str, Any]], persona: Any, re_asked: bool = False
) -> dict[str, str]:
    """
    Compose the confirmation prompt as both chat text and a TTS-ready sentence,
    so a pending CONFIRMATION_REQUIRED tool call can be spoken and answered by
    voice instead of only shown as a card the user has to click.

    ``re_asked`` marks an action whose earlier approval window lapsed (see
    ``app.agent.confirmations``), so the user hears why they are being asked again.
    """
    from app.awareness.briefing import address_suffix

    if not pending:
        return {"text": "", "spoken": ""}

    address = address_suffix(persona)
    descriptions = [_describe_pending_action(p) for p in pending]

    if len(descriptions) == 1:
        ask = f"I need your approval to {descriptions[0]}."
        lines = [f"**Confirmation required** — {descriptions[0]} ({pending[0].get('risk_tier', 'CONFIRMATION_REQUIRED')})"]
    else:
        ask = f"I need your approval for {len(descriptions)} actions: " + "; ".join(descriptions) + "."
        lines = ["**Confirmation required**"] + [
            f"- {d} ({p.get('risk_tier', 'CONFIRMATION_REQUIRED')})" for d, p in zip(descriptions, pending)
        ]

    if re_asked:
        ask = f"That approval had timed out, so I am asking again. {ask}"
        lines.insert(0, "_The earlier approval timed out, so this is being asked again._")
    spoken = f"{ask} Say yes to proceed, or no to cancel{address}."
    return {"text": "\n".join(lines), "spoken": spoken}


@dataclass
class BatchPermissionResult:
    all_allowed: bool
    approved_actions: list[PermissionDecision] = field(default_factory=list)
    pending_confirmations: list[PermissionDecision] = field(default_factory=list)


REASON_TOOLS = frozenset({"write_file", "patch_file", "delete_file", "execute_command"})


def _resolve_for_card(path: Any, workspace_path: Optional[str | Path]) -> Optional[Path]:
    raw = str(path or "").strip()
    if not raw:
        return None
    try:
        root = Path(workspace_path or WORKSPACE_ROOT).resolve()
        p = Path(raw)
        return p.resolve() if p.is_absolute() else (root / p).resolve()
    except Exception:
        return None


def _outside_note(target: Optional[Path], workspace_path: Optional[str | Path]) -> str:
    if target is None:
        return ""
    root = Path(workspace_path or WORKSPACE_ROOT).resolve()
    return "" if (target == root or root in target.parents) else f" It is outside this workspace: {target}."


def evaluate_tool_permission(
    tool_name: str,
    arguments: dict[str, Any],
    approved_action_ids: Optional[list[str]] = None,
    chat_mode: ChatMode = ChatMode.WORKSPACE,
    workspace_path: Optional[str | Path] = None,
    url_provenance: Optional[str] = None,
    user_message: Optional[str] = None,
) -> PermissionDecision:
    """
    Evaluate permission for a single tool call.
    Default policy: If tool is not in BASE_TOOL_RISK_MAP, default to CONFIRMATION_REQUIRED.
    ``url_provenance`` (from ``url_provenance_text``) enables the fetch_url origin check; None
    skips it. ``user_message`` is what the user typed this turn: reading the clipboard runs
    without asking only when it mentions the clipboard.
    """
    approved_ids = set(approved_action_ids or [])
    action_id = generate_action_id(tool_name, arguments)

    # 1. Base lookup (unclassified defaults to CONFIRMATION_REQUIRED)
    base_tier = BASE_TOOL_RISK_MAP.get(tool_name, RiskTier.CONFIRMATION_REQUIRED)
    effective_tier = base_tier
    custom_reason: Optional[str] = None

    # 2. Argument-aware dynamic rule optimizations
    if tool_name == "execute_command":
        cmd = arguments.get("command") or arguments.get("cmd") or arguments.get("command_line") or ""
        # The command decides the tier outright: a vetted plain read-only command runs, the rest
        # ask. (This used to keep the base tier whenever the command was safe, so every command
        # asked -- and the prefix check it guarded would have let `dir && del x` through.)
        effective_tier = evaluate_command_argument_risk(str(cmd), workspace_root=workspace_path)
    elif tool_name == "get_clipboard":
        if user_message is not None and _CLIPBOARD_CUE_RE.search(user_message):
            effective_tier = RiskTier.LOW_RISK
        else:
            custom_reason = ("You didn't mention the clipboard, and it can hold passwords or "
                             "other private text.")
    elif tool_name == "delete_file":
        effective_tier = RiskTier.HIGH_RISK
        target = _resolve_for_card(arguments.get("file_path") or arguments.get("path") or "", workspace_path)
        if target is not None and target.is_dir():
            count = sum(1 for f in target.rglob("*") if f.is_file())
            custom_reason = f"Moves the folder '{target.name}' and the {count} file(s) in it to the Recycle Bin."
        else:
            custom_reason = "Moves it to the Recycle Bin (restorable from there)."
        custom_reason += _outside_note(target, workspace_path)
    elif tool_name in ("write_file", "patch_file"):
        path = arguments.get("file_path") or arguments.get("path") or ""
        path_tier = evaluate_file_path_risk(str(path), is_write_or_delete=True, chat_mode=chat_mode, workspace_root=workspace_path)
        if path_tier != RiskTier.LOW_RISK or base_tier == RiskTier.LOW_RISK:
            effective_tier = path_tier
        target = _resolve_for_card(path, workspace_path)
        overwrite = str(arguments.get("overwrite", True)).strip().lower() not in ("false", "0", "no")
        if (tool_name == "write_file" and overwrite and target is not None and target.is_file()
                and effective_tier == RiskTier.LOW_RISK):
            # Creating a file runs; replacing one asks (the old version is backed up first).
            effective_tier = RiskTier.CONFIRMATION_REQUIRED
            custom_reason = f"Replaces the existing '{target.name}' (the old version is backed up first)."
        elif effective_tier != RiskTier.LOW_RISK:
            custom_reason = ("Writes outside this workspace" if _outside_note(target, workspace_path)
                             else "Changes a protected location") + (f": {target}" if target else "") + "."
    elif tool_name in ("read_file", "list_directory", "find_files", "grep_in_files"):
        path = (arguments.get("file_path") or arguments.get("directory_path") or arguments.get("path")
                or arguments.get("root_dir") or "")
        path_tier = evaluate_file_path_risk(str(path), is_write_or_delete=False, chat_mode=chat_mode, workspace_root=workspace_path)
        if path_tier != RiskTier.LOW_RISK:
            effective_tier = path_tier
            target = _resolve_for_card(path, workspace_path)
            custom_reason = f"Reads outside this workspace: {target}." if target else None
    elif tool_name == "fetch_url":
        url = arguments.get("url") or arguments.get("target_url") or arguments.get("link") or ""
        url_tier = evaluate_url_risk(str(url))
        if url_tier != RiskTier.LOW_RISK:
            effective_tier = url_tier
        elif url_provenance is not None and not url_came_from_conversation(str(url), url_provenance):
            effective_tier = RiskTier.CONFIRMATION_REQUIRED
            custom_reason = (
                "This address didn't come from you, a search result or a page already opened in "
                "this chat, so it could carry data out of the conversation."
            )
    elif tool_name == "launch_app":
        target = arguments.get("name_or_path") or arguments.get("name") or arguments.get("app") or ""
        la_tier, la_reason = evaluate_launch_app_risk(str(target))
        effective_tier = la_tier
        custom_reason = la_reason
    elif tool_name == "kill_process":
        target = arguments.get("pid_or_name") or arguments.get("pid") or arguments.get("name") or arguments.get("process_name") or ""
        kp_tier, kp_reason = evaluate_kill_process_risk(target)
        if kp_tier != RiskTier.LOW_RISK or base_tier == RiskTier.LOW_RISK:
            effective_tier = kp_tier
            custom_reason = kp_reason
        if str(arguments.get("force", "")).strip().lower() in ("true", "1", "yes"):
            custom_reason = f"{custom_reason} Force-ends it at once: anything unsaved is lost.".strip()
        else:
            custom_reason = f"{custom_reason} Asks it to close, as clicking X does; it may ask to save.".strip()

    # The model's own "why" (write/patch/delete/execute_command), shown on the card.
    model_reason = str(arguments.get("reason") or "").strip()
    if model_reason and tool_name in REASON_TOOLS:
        custom_reason = f"{custom_reason or ''} Jarvis: {model_reason[:200]}".strip()

    # 3. Check if user already provided explicit approval token (strictly per action_id)
    if action_id in approved_ids:
        return PermissionDecision(
            tool=tool_name,
            args=arguments,
            risk_tier=effective_tier,
            allowed=True,
            action_id=action_id,
            reason="Explicitly approved by user approval token."
        )

    # 4. Determine authorization based on tier
    if effective_tier == RiskTier.LOW_RISK:
        return PermissionDecision(
            tool=tool_name,
            args=arguments,
            risk_tier=effective_tier,
            allowed=True,
            action_id=action_id,
            reason=custom_reason or "Tool categorized as LOW_RISK. Auto-approved."
        )
    else:
        return PermissionDecision(
            tool=tool_name,
            args=arguments,
            risk_tier=effective_tier,
            allowed=False,
            action_id=action_id,
            reason=custom_reason or f"Action requires user confirmation (Risk Tier: {effective_tier.value})."
        )


def evaluate_tool_calls_batch(
    tool_calls: list[dict[str, Any]],
    approved_action_ids: Optional[list[str]] = None,
    chat_mode: ChatMode = ChatMode.WORKSPACE,
    workspace_path: Optional[str | Path] = None,
    url_provenance: Optional[str] = None,
    user_message: Optional[str] = None,
) -> BatchPermissionResult:
    """
    Batch evaluate multiple tool calls in a single pass to prevent fragmented confirmation prompts.
    """
    approved: list[PermissionDecision] = []
    pending: list[PermissionDecision] = []

    for tc in tool_calls:
        fn_name = tc.get("name", "")
        fn_args = tc.get("args", {})
        decision = evaluate_tool_permission(
            fn_name,
            fn_args,
            approved_action_ids,
            chat_mode=chat_mode,
            workspace_path=workspace_path,
            url_provenance=url_provenance,
            user_message=user_message,
        )

        if decision.allowed:
            approved.append(decision)
        else:
            pending.append(decision)

    all_allowed = len(pending) == 0
    return BatchPermissionResult(
        all_allowed=all_allowed,
        approved_actions=approved,
        pending_confirmations=pending
    )

