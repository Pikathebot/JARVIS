"""
Tool execution pipeline: off the event loop, under a timeout, results trimmed; and image
offload when a request with pictures does not fit the context.
"""
import asyncio
import threading
import time

import pytest

from app.agent.image_offload import (
    PLACEHOLDER,
    has_image_parts,
    looks_like_context_overflow,
    offload_images,
)
from app.agent.tool_pipeline import ToolOutcome, ToolPipeline, truncate_oversized


@pytest.fixture
def pipeline():
    return ToolPipeline(default_timeout=0.3, timeouts={"slow_ok": 2.0}, post_hooks=[])


# ---------------------------------------------------------------- execute

@pytest.mark.asyncio
async def test_native_tool_runs_on_a_worker_thread_and_reports_duration(pipeline):
    loop_thread = threading.get_ident()
    seen = {}

    def tool():
        seen["thread"] = threading.get_ident()
        time.sleep(0.05)
        return "listing"

    outcome = await pipeline.run("list_directory", {"p": "."}, native=tool)

    assert outcome.ok and outcome.result == "listing"
    assert seen["thread"] != loop_thread
    assert outcome.duration_ms >= 40


@pytest.mark.asyncio
async def test_slow_native_tool_times_out_without_blocking_the_loop(pipeline):
    def tool():
        time.sleep(1.0)
        return "never seen"

    ticks = 0

    async def ticker():
        nonlocal ticks
        for _ in range(5):
            await asyncio.sleep(0.02)
            ticks += 1

    outcome, _ = await asyncio.gather(pipeline.run("execute_command", {}, native=tool), ticker())

    assert outcome.timed_out and not outcome.ok
    assert "timed out after 0 seconds" in outcome.result or "timed out" in outcome.result
    assert ticks == 5  # the loop kept running while the tool slept


@pytest.mark.asyncio
async def test_per_tool_timeout_overrides_the_default(pipeline):
    def tool():
        time.sleep(0.5)
        return "done"

    outcome = await pipeline.run("slow_ok", {}, native=tool)
    assert outcome.ok and outcome.result == "done"


@pytest.mark.asyncio
async def test_mcp_coroutine_tools_go_through_the_same_timeout(pipeline):
    async def hangs():
        await asyncio.sleep(5)
        return "x"

    outcome = await pipeline.run("mcp.thing", {}, coroutine=hangs)
    assert outcome.timed_out


@pytest.mark.asyncio
async def test_exceptions_and_error_strings_are_both_errors(pipeline):
    def raises():
        raise RuntimeError("boom")

    def error_string():
        return "Error: no such file"

    assert (await pipeline.run("a", {}, native=raises)).error
    assert "boom" in (await pipeline.run("a", {}, native=raises)).result
    assert (await pipeline.run("b", {}, native=error_string)).error


# ------------------------------------------------------------- post hooks

def test_truncate_keeps_head_and_tail():
    outcome = ToolOutcome(tool="execute_command", args={}, result="H" * 900 + "M" * 1000 + "T" * 300)

    replaced = truncate_oversized(outcome, max_chars=1000)

    assert replaced.startswith("H" * 700)
    assert replaced.endswith("T" * 300)
    assert "characters omitted from the middle" in replaced
    assert outcome.truncated is True


def test_truncate_leaves_small_results_alone():
    outcome = ToolOutcome(tool="x", args={}, result="short")
    assert truncate_oversized(outcome, max_chars=1000) is None
    assert outcome.truncated is False


@pytest.mark.asyncio
async def test_post_hooks_run_in_order_and_can_replace_or_abstain():
    calls = []

    def upper(o):
        calls.append("upper")
        return o.result.upper()

    def abstain(o):
        calls.append("abstain")
        return None

    def block_secrets(o):
        calls.append("block")
        return "[blocked]" if "SECRET" in o.result else None

    p = ToolPipeline(post_hooks=[("upper", upper), ("abstain", abstain), ("block", block_secrets)])
    outcome = await p.run("t", {}, native=lambda: "the secret")

    assert calls == ["upper", "abstain", "block"]
    assert outcome.result == "[blocked]"
    assert outcome.hooks_applied == ["upper", "block"]


@pytest.mark.asyncio
async def test_a_failing_hook_does_not_lose_the_result():
    def bad(o):
        raise ValueError("hook bug")

    p = ToolPipeline(post_hooks=[("bad", bad)])
    outcome = await p.run("t", {}, native=lambda: "fine")
    assert outcome.result == "fine"


@pytest.mark.asyncio
async def test_default_pipeline_trims_oversized_output(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "tool_result_max_chars", 200)
    outcome = await ToolPipeline().run("t", {}, native=lambda: "x" * 5000)

    assert outcome.truncated and len(outcome.result) < 400
    assert outcome.to_event()["status"] == "success"
    assert outcome.to_event()["truncated"] is True


def test_to_event_status_reflects_timeout():
    outcome = ToolOutcome(tool="t", args={"a": 1}, result="Error: timed out", timed_out=True)
    assert outcome.to_event()["status"] == "timeout"


# ----------------------------------------------------------- image offload

def image_message(text="look"):
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
    }


def test_overflow_is_recognised_from_llama_server_error_text():
    assert looks_like_context_overflow(400, '{"error":{"message":"the request exceeds the available context size"}}')
    assert looks_like_context_overflow(500, "failed to process image")
    assert not looks_like_context_overflow(200, "")
    assert not looks_like_context_overflow(503, "loading model")


def test_offload_replaces_images_with_placeholders_and_flattens_to_text():
    messages = [{"role": "system", "content": "sys"}, image_message("what is this")]

    stripped, count = offload_images(messages)

    assert count == 1
    assert stripped[0] == messages[0]
    assert stripped[1]["content"] == f"what is this\n{PLACEHOLDER}"
    assert has_image_parts(messages) and not has_image_parts(stripped)


def test_offload_leaves_plain_messages_untouched():
    messages = [{"role": "user", "content": "hi"}]
    assert offload_images(messages) == (messages, 0)


def test_provider_rewrites_payload_only_for_image_overflow():
    from app.agent.llamacpp_provider import LlamaCppProvider

    payload = {"messages": [image_message()]}
    assert LlamaCppProvider._offload_images_if_overflow(payload, 400, "exceeds the available context size")
    assert not has_image_parts(payload["messages"])

    # Already stripped, or a different error: no second rewrite.
    assert not LlamaCppProvider._offload_images_if_overflow(payload, 400, "exceeds the available context size")
    assert not LlamaCppProvider._offload_images_if_overflow({"messages": [image_message()]}, 503, "loading model")
