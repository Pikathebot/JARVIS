import json
import logging
from typing import Any, Optional
from pydantic import BaseModel, ValidationError

logger = logging.getLogger("jarvis.agent.validator")


class ValidationResult(BaseModel):
    valid: bool
    tool_name: str
    args: dict[str, Any]
    error: Optional[str] = None
    repair_attempt: int = 0  # 0 = first attempt, 1 = post-repair retry


async def validate_tool_call(
    tool_name: str,
    raw_args: dict[str, Any],
    schema: Optional[type[BaseModel]] = None,
    repair_attempt: int = 0
) -> ValidationResult:
    """
    Validate raw tool arguments against a Pydantic schema.
    If schema is None, passes through as valid.
    On ValidationError, returns ValidationResult(valid=False, error=<formatted error>).
    """
    if not isinstance(raw_args, dict):
        return ValidationResult(
            valid=False,
            tool_name=tool_name,
            args={},
            error=f"Tool arguments must be a dictionary/object, received: {type(raw_args).__name__}",
            repair_attempt=repair_attempt
        )

    if schema is None:
        return ValidationResult(
            valid=True,
            tool_name=tool_name,
            args=raw_args,
            repair_attempt=repair_attempt
        )

    try:
        validated = schema.model_validate(raw_args)
        return ValidationResult(
            valid=True,
            tool_name=tool_name,
            args=validated.model_dump(),
            repair_attempt=repair_attempt
        )
    except ValidationError as ve:
        # Format human/LLM-readable error list
        formatted_errors = []
        for err in ve.errors():
            loc = ".".join(str(elem) for elem in err.get("loc", []))
            msg = err.get("msg", "Invalid parameter")
            formatted_errors.append(f"'{loc}': {msg}")
        error_summary = "; ".join(formatted_errors)
        logger.warning("Tool call '%s' validation failed: %s", tool_name, error_summary)
        return ValidationResult(
            valid=False,
            tool_name=tool_name,
            args=raw_args,
            error=error_summary,
            repair_attempt=repair_attempt
        )
    except Exception as e:
        logger.warning("Unexpected error validating tool call '%s': %s", tool_name, e)
        return ValidationResult(
            valid=False,
            tool_name=tool_name,
            args=raw_args,
            error=str(e),
            repair_attempt=repair_attempt
        )


class CallHistory:
    """
    Per-turn call history tracker to break repeated duplicate invocation loops.
    """

    def __init__(self, window: int = 3):
        self.window = max(1, window)
        self.history: list[tuple[str, str]] = []
        # Whole-turn counts for the result hints (PLAN 4.15 #4). They live here because a turn
        # paused for approval parks and restores this object, so a resumed turn keeps counting.
        self.failures: dict[tuple[str, str], int] = {}
        self.web_calls = 0

    def hint_for(self, tool_name: str, result: str, invalid_args: bool = False) -> str:
        """The line to append to this call's result for the model, or "" (tool_errors)."""
        from app.agent.tool_errors import NEXT_STEP, WEB_FAMILY, WEB_HINT_AT, error_kind, stop_line, web_hint

        lines = []
        kind = "bad_input" if invalid_args else error_kind(result)
        if kind:
            seen = self.failures.get((tool_name, kind), 0) + 1
            self.failures[(tool_name, kind)] = seen
            lines.append(stop_line(tool_name) if seen >= 2 else NEXT_STEP[kind])
        if tool_name in WEB_FAMILY:
            self.web_calls += 1
            if self.web_calls >= WEB_HINT_AT:
                lines.append(web_hint(self.web_calls))
        return " ".join(line for line in lines if line)

    def canonicalize_args(self, args: Any) -> str:
        """
        Canonicalize args dictionary into a deterministic sorted JSON string.
        """
        if not isinstance(args, dict):
            return json.dumps(args, sort_keys=True)
        try:
            return json.dumps(args, sort_keys=True)
        except Exception:
            return str(args)

    def record(self, tool_name: str, args: dict[str, Any]) -> None:
        """
        Record a tool invocation in call history.
        """
        canonical_args = self.canonicalize_args(args)
        self.history.append((tool_name, canonical_args))
        if len(self.history) > self.window:
            self.history.pop(0)

    def is_duplicate(self, tool_name: str, args: dict[str, Any]) -> bool:
        """
        True if the same (tool_name, canonicalized args) appeared in the
        immediately preceding call within window.
        """
        if not self.history:
            return False

        canonical_args = self.canonicalize_args(args)
        # Check if the immediately preceding call matches
        last_tool, last_args = self.history[-1]
        return last_tool == tool_name and last_args == canonical_args
