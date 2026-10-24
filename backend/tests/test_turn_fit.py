"""One request inside the model's window: tools priced in, a turn's results fitted (PLAN 4.23c)."""
from tests.test_context_manager import memory_test_env  # noqa: F401  (fixture)
from app.agent.tools.registry import tools_for_space
from app.memory.context_manager import ContextManager
from app.memory.turn_fit import MIN_RESULT_CHARS, fit_messages, tools_tokens
from app.memory.token_counter import TokenCounter

counter = TokenCounter()


def _turn(history_pairs=0, result_chars=0):
    msgs = [{"role": "system", "content": "head " * 200}]
    for i in range(history_pairs):
        msgs += [{"role": "user", "content": f"old question {i} " * 50},
                 {"role": "assistant", "content": f"old answer {i} " * 50}]
    msgs.append({"role": "user", "content": "what are wuchang's system requirements?"})
    msgs.append({"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "fetch_url", "arguments": {"url": "x"}}}]})
    msgs.append({"role": "tool", "tool_call_id": "c1", "name": "fetch_url", "content": "page text " * (result_chars // 10)})
    return msgs


def test_the_tools_cost_thousands_of_tokens():
    # the head's schemas: ~20k chars in Freeform, the part the budget used to leave out
    assert tools_tokens(tools_for_space("FREEFORM")) > 4000


def test_a_request_that_fits_is_left_alone():
    msgs = _turn(history_pairs=2, result_chars=2000)
    fitted, done = fit_messages(msgs, window=32768, tools_cost=6000)
    assert fitted is msgs and done == []


def test_the_biggest_tool_result_is_cut_first():
    msgs = _turn(history_pairs=2, result_chars=60000)
    fitted, done = fit_messages(msgs, window=16384, tools_cost=6000, reserve=2048)
    assert done == ["cut 1 tool result"]
    result = fitted[-1]["content"]
    assert len(result) < 60000 and len(result) >= MIN_RESULT_CHARS and "cut to fit the context window" in result
    assert counter.count_messages(fitted) <= 16384 - 2048 - 6000
    assert [m["role"] for m in fitted] == [m["role"] for m in msgs]  # nothing dropped


def test_old_exchanges_go_only_after_results_and_never_the_last_two():
    msgs = _turn(history_pairs=40, result_chars=3000)
    fitted, done = fit_messages(msgs, window=8192, tools_cost=2000, reserve=1024)
    assert any(d.startswith("dropped") for d in done)
    assert fitted[0]["role"] == "system"
    assert fitted[-3]["content"].startswith("what are wuchang")  # the request and its call/result
    assert fitted[-2].get("tool_calls") and fitted[-1]["role"] == "tool"
    assert counter.count_messages(fitted) <= 8192 - 1024 - 2000


def test_the_budget_reserves_the_tools(memory_test_env):
    store, _ = memory_test_env
    for i in range(60):
        store.append_message("fit", role="user", content=f"question {i} " * 120)
        store.append_message("fit", role="assistant", content=f"answer {i} " * 120)
    cm = ContextManager(memory_store=store)
    plain = cm.build_context(session_id="fit", user_message="hi", system_prompt="sys", max_context_tokens=16384)
    priced = cm.build_context(session_id="fit", user_message="hi", system_prompt="sys", max_context_tokens=16384,
                              tools_tokens=6000)
    assert priced.budget_report["tools_tokens"] == 6000
    assert priced.budget_report["tier4_history_tokens"] < plain.budget_report["tier4_history_tokens"] - 4000
    assert priced.budget_report["total_input_tokens_used"] <= 16384 - 2048
