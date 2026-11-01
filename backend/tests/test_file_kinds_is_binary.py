import os

import pytest

from app.agent.tools.file_kinds import SNIFF_BYTES, is_binary


def _reference(path) -> bool:
    """The original per-byte implementation, kept as the oracle."""
    with open(path, "rb") as f:
        head = f.read(SNIFF_BYTES)
    if not head:
        return False
    if b"\x00" in head:
        return True
    text_like = sum(1 for b in head if b in (9, 10, 13) or 32 <= b < 127 or b >= 128)
    return text_like / len(head) < 0.7


@pytest.mark.parametrize("name,data", [
    ("empty", b""),
    ("ascii", b"def f(x):\n    return x\n" * 100),
    ("utf8", "héllo wörld 日本語\n".encode() * 200),
    ("nul", b"abc\x00def"),
    ("random", os.urandom(SNIFF_BYTES)),
    ("controls", bytes([1, 2, 3, 4, 5, 6, 7, 8, 11, 12, 14, 127]) * 300 + b"text"),
    ("mostly_text_some_ctrl", b"hello world " * 100 + bytes([1, 2, 3]) * 10),
])
def test_matches_reference(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data)
    assert is_binary(p) == _reference(p)


def test_missing_file_is_not_binary(tmp_path):
    assert is_binary(tmp_path / "nope") is False
