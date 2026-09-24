import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import httpx

from app.agent.llamacpp_provider import LlamaCppProvider, strip_thinking_tags
from app.agent.tools.registry import read_file, web_search


def test_strip_thinking_tags():
    raw = "<think>I need to search for quantum computing info.</think>Quantum computing is rapidly advancing."
    clean, reasoning = strip_thinking_tags(raw)
    assert clean == "Quantum computing is rapidly advancing."
    assert "search for quantum computing" in reasoning


@pytest.mark.anyio
async def test_health_check_true_on_200():
    provider = LlamaCppProvider(base_url="http://127.0.0.1:8001")
    mock_resp = MagicMock(status_code=200)
    with patch.object(provider, "_port_listening", new_callable=AsyncMock, return_value=True),          patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_resp
        result = await provider.health_check()
        assert result is True


@pytest.mark.anyio
async def test_health_check_skips_http_when_nothing_listens():
    # A refused localhost connect takes ~2 s on Windows; a closed port must answer False from
    # the listener-table check without an HTTP request.
    provider = LlamaCppProvider(base_url="http://127.0.0.1:8001")
    with patch.object(provider, "_port_listening", new_callable=AsyncMock, return_value=False),          patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        assert await provider.health_check() is False
        mock_get.assert_not_called()


@pytest.mark.anyio
async def test_health_check_false_on_error():
    provider = LlamaCppProvider(base_url="http://127.0.0.1:8001")
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = httpx.ConnectError("Connection refused")
        result = await provider.health_check()
        assert result is False


@pytest.mark.anyio
async def test_chat_returns_normalized_message():
    provider = LlamaCppProvider(base_url="http://127.0.0.1:8001")
    mock_api_resp = {
        "id": "chatcmpl-test",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "Hello world!"
                }
            }
        ]
    }
    mock_resp = MagicMock(status_code=200, json=lambda: mock_api_resp)
    with patch.object(provider, "_ensure_server_ready", new_callable=AsyncMock), \
         patch.object(provider, "health_check", return_value=True), \
         patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        res = await provider.chat(messages=[{"role": "user", "content": "Hi"}], model="main")
        assert res["message"]["role"] == "assistant"
        assert res["message"]["content"] == "Hello world!"
        assert res["message"]["tool_calls"] is None


@pytest.mark.anyio
async def test_chat_parses_tool_calls_and_handles_malformed_arguments():
    provider = LlamaCppProvider(base_url="http://127.0.0.1:8001")
    mock_api_resp = {
        "id": "chatcmpl-tools",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "<think>planning tool call</think>",
                    "tool_calls": [
                        {
                            "id": "call_valid",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"file_path": "test.txt"}'
                            }
                        },
                        {
                            "id": "call_malformed",
                            "type": "function",
                            "function": {
                                "name": "execute_command",
                                "arguments": '{malformed_json}'
                            }
                        }
                    ]
                }
            }
        ]
    }
    mock_resp = MagicMock(status_code=200, json=lambda: mock_api_resp)
    with patch.object(provider, "_ensure_server_ready", new_callable=AsyncMock), \
         patch.object(provider, "health_check", return_value=True), \
         patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        res = await provider.chat(messages=[{"role": "user", "content": "Read file"}], model="main", tools=[read_file])

        assert res["message"]["content"] == ""
        assert "planning tool call" in res["message"]["reasoning_content"]
        assert len(res["message"]["tool_calls"]) == 2

        tc1 = res["message"]["tool_calls"][0]
        assert tc1["id"] == "call_valid"
        assert tc1["function"]["name"] == "read_file"
        assert tc1["function"]["arguments"] == {"file_path": "test.txt"}

        tc2 = res["message"]["tool_calls"][1]
        assert tc2["id"] == "call_malformed"
        assert tc2["function"]["name"] == "execute_command"
        assert tc2["function"]["arguments"] == {}  # Graceful fallback on malformed JSON


@pytest.mark.anyio
async def test_stream_accumulates_tool_calls_and_yields_deltas():
    provider = LlamaCppProvider(base_url="http://127.0.0.1:8001")

    # Simulate SSE lines
    sse_lines = [
        'data: {"choices":[{"delta":{"content":"Searching"}}]}',
        'data: {"choices":[{"delta":{"content":" for data..."}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_stream_1","function":{"name":"web_search","arguments":"{\\"query\\":"}}]}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":" \\"python 3.12\\"}"}}]}}]}',
        'data: [DONE]'
    ]

    async def mock_aiter_lines():
        for line in sse_lines:
            yield line

    mock_stream_resp = MagicMock(status_code=200)
    mock_stream_resp.aiter_lines = mock_aiter_lines

    class MockStreamContext:
        async def __aenter__(self):
            return mock_stream_resp
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    with patch.object(provider, "_ensure_server_ready", new_callable=AsyncMock), \
         patch.object(provider, "health_check", return_value=True), \
         patch("httpx.AsyncClient.stream", return_value=MockStreamContext()):
        events = []
        async for ev in provider.stream_chat(messages=[{"role": "user", "content": "Search"}], model="main", tools=[web_search]):
            events.append(ev)

        # Verify text deltas. Exact chunk boundaries are no longer guaranteed to match the SSE
        # deltas one-to-one: ThinkTagStreamScanner holds back a trailing fragment that could still
        # turn into a <think>/</think> tag, so the same text can arrive split differently than it
        # was sent in. The joined text is what matters.
        text_deltas = [e["content"] for e in events if e["event"] == "text_delta"]
        assert "".join(text_deltas) == "Searching for data..."

        # Verify accumulated tool call
        tool_call_events = [e["tool_call"] for e in events if e["event"] == "tool_call"]
        assert len(tool_call_events) == 1
        assert tool_call_events[0]["id"] == "call_stream_1"
        assert tool_call_events[0]["function"]["name"] == "web_search"
        assert tool_call_events[0]["function"]["arguments"] == {"query": "python 3.12"}

        # Verify done event
        done_events = [e for e in events if e["event"] == "done"]
        assert len(done_events) == 1
        assert done_events[0]["raw"]["content"] == "Searching for data..."


@pytest.mark.anyio
async def test_sampling_profile_parameters_sent_correctly():
    provider = LlamaCppProvider(base_url="http://127.0.0.1:8001")
    captured_payloads = []

    mock_resp = MagicMock(status_code=200, json=lambda: {"choices": [{"message": {"role": "assistant", "content": "ok"}}]})

    async def capture_post(url, json=None, headers=None):
        captured_payloads.append(json)
        return mock_resp

    with patch.object(provider, "_ensure_server_ready", new_callable=AsyncMock), \
         patch.object(provider, "health_check", return_value=True), \
         patch("httpx.AsyncClient.post", side_effect=capture_post):
        # 1. Profile general
        await provider.chat(messages=[{"role": "user", "content": "hi"}], profile="general")
        assert captured_payloads[0]["temperature"] == 0.7
        assert captured_payloads[0]["top_p"] == 0.8
        assert captured_payloads[0]["top_k"] == 20
        assert captured_payloads[0]["presence_penalty"] == 1.5

        # 2. Profile coding
        await provider.chat(messages=[{"role": "user", "content": "write code"}], profile="coding")
        assert captured_payloads[1]["temperature"] == 0.6
        assert captured_payloads[1]["top_p"] == 0.95
        assert captured_payloads[1]["top_k"] == 20
        assert captured_payloads[1]["presence_penalty"] == 0.0


@pytest.mark.anyio
async def test_unload_model_delegates_to_process_manager():
    mock_pm = MagicMock()
    mock_pm.stop = AsyncMock(return_value=True)
    provider = LlamaCppProvider(process_manager=mock_pm)

    result = await provider.unload_model()
    assert result is True
    mock_pm.stop.assert_awaited_once()

    # Never raises on exception
    mock_pm.stop.side_effect = Exception("Process stop error")
    safe_result = await provider.unload_model()
    assert safe_result is False
