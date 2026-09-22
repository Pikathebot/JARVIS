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
    "get_clipboard": RiskTier.CONFIRMATION_REQUIRED,
    "set_clipboard": RiskTier.CONFIRMATION_REQUIRED,
    "list_processes": RiskTier.LOW_RISK,
    "kill_process": RiskTier.LOW_RISK,
    "send_toast": RiskTier.LOW_RISK,
    "play_audio": RiskTier.LOW_RISK,
    "stop_playback": RiskTier.LOW_RISK,
    "get_system_status": RiskTier.LOW_RISK,
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



# Fast compiled regex patterns for argument-aware dynamic risk analysis
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

DANGEROUS_COMMAND_PATTERNS = re.compile(
    r"\b(rm\s+-rf|del\s+/[sSfFqQ]|format|diskpart|shutdown|mkfs|rmdir\s+/s)\b",
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


def evaluate_command_argument_risk(command: str) -> RiskTier:
    """
    Argument-aware optimization for command execution:
    Safe read-only commands downgrade to LOW_RISK for zero friction;
    Destructive commands escalate to HIGH_RISK.
    """
    cmd_clean = str(command).strip().strip("'\"")

    # Destructive pattern check
    if DANGEROUS_COMMAND_PATTERNS.search(cmd_clean):
        return RiskTier.HIGH_RISK

    cmd_lower = cmd_clean.lower().replace("\\", "/")
    # Known safe read-only prefix check
    for prefix in SAFE_COMMAND_PREFIXES:
        if cmd_lower.startswith(prefix):
            return RiskTier.LOW_RISK

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
    if any(marker in target_l for marker in ("\downloads\\", "\temp\\", "\tmp\\", "\appdata\local\temp\\")):
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
    4. Single non-major instance or explicit non-major PID -> LOW_RISK.
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
            return RiskTier.LOW_RISK, "Explicit single PID termination."
        except Exception:
            return RiskTier.LOW_RISK, "PID lookup failed, defaulting to LOW_RISK."

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

    return RiskTier.LOW_RISK, None



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
    for key in ("command", "cmd", "file_path", "path", "url", "pid_or_name", "process_name", "name"):
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


def evaluate_tool_permission(
    tool_name: str,
    arguments: dict[str, Any],
    approved_action_ids: Optional[list[str]] = None,
    chat_mode: ChatMode = ChatMode.WORKSPACE,
    workspace_path: Optional[str | Path] = None
) -> PermissionDecision:
    """
    Evaluate permission for a single tool call.
    Default policy: If tool is not in BASE_TOOL_RISK_MAP, default to CONFIRMATION_REQUIRED.
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
        cmd_tier = evaluate_command_argument_risk(str(cmd))
        if base_tier == RiskTier.LOW_RISK or cmd_tier != RiskTier.LOW_RISK:
            effective_tier = cmd_tier
    elif tool_name == "delete_file":
        effective_tier = RiskTier.HIGH_RISK
    elif tool_name in ("write_file", "patch_file"):
        path = arguments.get("file_path") or arguments.get("path") or ""
        path_tier = evaluate_file_path_risk(str(path), is_write_or_delete=True, chat_mode=chat_mode, workspace_root=workspace_path)
        if path_tier != RiskTier.LOW_RISK or base_tier == RiskTier.LOW_RISK:
            effective_tier = path_tier
    elif tool_name in ("read_file", "list_directory", "find_files", "grep_in_files"):
        path = arguments.get("file_path") or arguments.get("path") or arguments.get("root_dir") or ""
        path_tier = evaluate_file_path_risk(str(path), is_write_or_delete=False, chat_mode=chat_mode, workspace_root=workspace_path)
        if path_tier != RiskTier.LOW_RISK:
            effective_tier = path_tier
    elif tool_name == "fetch_url":
        url = arguments.get("url") or arguments.get("target_url") or arguments.get("link") or ""
        url_tier = evaluate_url_risk(str(url))
        if url_tier != RiskTier.LOW_RISK:
            effective_tier = url_tier
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
    workspace_path: Optional[str | Path] = None
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
            workspace_path=workspace_path
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

