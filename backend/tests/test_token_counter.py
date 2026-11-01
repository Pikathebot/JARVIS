import math

import pytest

from app.memory.token_counter import TokenCounter


def _reference(text: str, cpt: float = 3.8) -> int:
    """The original per-character implementation, kept as the oracle."""
    non_ascii = sum(1 for ch in text if ord(ch) > 127)
    return max(1, math.ceil((len(text) - non_ascii) / cpt) + non_ascii)


@pytest.mark.parametrize("text", [
    "hello world",
    "def f(x):\n    return x + 1\n" * 50,
    "héllo wörld",
    "日本語のテキスト",
    "emoji 🎉 mixed ascii",
    "�" * 20 + "binary-ish",
    "x",
])
def test_matches_reference(text):
    assert TokenCounter().count(text) == _reference(text)


def test_empty_is_zero():
    assert TokenCounter().count("") == 0


def test_non_ascii_costs_one_token_per_char():
    c = TokenCounter()
    assert c.count("日本語") == 3
