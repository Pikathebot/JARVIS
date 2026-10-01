"""The tool description the model sees is the docstring's whole first paragraph, not its first
line: get_system_status wrapped, and the model never saw "the only hardware numbers you may quote"."""
from app.agent.tool_schema import convert_tool_to_openai_schema, tool_description
from app.agent.tools.registry import AVAILABLE_TOOLS


def test_description_is_the_first_paragraph_on_one_line():
    doc = "Measure the machine: GPU, RAM,\nfree disk. Call this whenever asked.\n\nMore detail.\n"
    assert tool_description(doc) == "Measure the machine: GPU, RAM, free disk. Call this whenever asked."
    assert tool_description("Do a thing\n  across lines.\nArgs:\n    x: y") == "Do a thing across lines."
    assert tool_description("") == ""


def test_get_system_status_keeps_its_instruction():
    from app.agent.tools.registry import get_system_status
    desc = convert_tool_to_openai_schema(get_system_status)["function"]["description"]
    assert "only hardware numbers you may quote" in desc


def test_no_tool_description_leaks_its_args_section_or_says_it_asks():
    tools = AVAILABLE_TOOLS.values() if isinstance(AVAILABLE_TOOLS, dict) else AVAILABLE_TOOLS
    for tool in tools:
        desc = convert_tool_to_openai_schema(tool)["function"]["description"]
        assert "Args:" not in desc and "\n" not in desc, tool.__name__
        # Whether a call asks is the permission gate's decision, argument by argument.
        assert "requires user confirmation" not in desc.lower(), tool.__name__
