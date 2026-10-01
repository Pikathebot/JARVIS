import inspect
import logging
import re
from typing import Any, Callable, Optional
from app.agent.tools.registry import TOOL_SCHEMAS

logger = logging.getLogger("jarvis.agent.tool_schema")

# The description the model sees is the docstring's first paragraph, joined onto one line: up to
# a blank line or an "Args:"-style section. It used to be the first *line*, so every docstring
# that wrapped lost its second half ("...free disk," without "call this whenever...").
_DESCRIPTION_END_RE = re.compile(r"\n\s*\n|\n\s*(?:Args|Arguments|Parameters|Returns|Raises|Examples?):")


def tool_description(doc: str) -> str:
    return " ".join(_DESCRIPTION_END_RE.split((doc or "").strip(), maxsplit=1)[0].split())


def convert_tool_to_openai_schema(tool: Any) -> Optional[dict[str, Any]]:
    """
    Convert a Python tool function, MCP tool dictionary, or schema object to OpenAI function format:
    {
        "type": "function",
        "function": {
            "name": str,
            "description": str,
            "parameters": dict
        }
    }
    """
    if tool is None:
        return None

    if isinstance(tool, dict):
        if "type" in tool and tool["type"] == "function" and "function" in tool:
            return tool
        if "name" in tool:
            params = tool.get("parameters") or tool.get("inputSchema") or {"type": "object", "properties": {}, "required": []}
            return {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", f"Execute {tool['name']}."),
                    "parameters": params
                }
            }
        return None

    if callable(tool):
        fn_name = getattr(tool, "__name__", str(tool))
        doc = inspect.getdoc(tool) or f"Execute {fn_name}."
        short_desc = tool_description(doc) or f"Execute {fn_name}."

        schema_cls = TOOL_SCHEMAS.get(fn_name)
        if schema_cls:
            raw_schema = schema_cls.model_json_schema()
            parameters = {
                "type": "object",
                "properties": raw_schema.get("properties", {}),
                "required": raw_schema.get("required", [])
            }
        else:
            parameters = {
                "type": "object",
                "properties": {},
                "required": []
            }

        return {
            "type": "function",
            "function": {
                "name": fn_name,
                "description": short_desc,
                "parameters": parameters
            }
        }

    return None
