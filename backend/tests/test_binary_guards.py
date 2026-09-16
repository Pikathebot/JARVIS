"""
An image indexed as text once poured 11k tokens of U+FFFD soup into an 8k context. Three
guards: the indexer refuses non-text files, the token estimate prices non-ASCII honestly,
and retrieval drops binary-looking chunks.
"""
from pathlib import Path

from app.memory.context_manager import ContextManager
from app.memory.token_counter import TokenCounter
from app.rag.indexer import ProjectIndexer, looks_binary


def test_looks_binary_flags_replacement_soup_and_nul_but_not_prose():
    assert looks_binary("\x89PNG\r\n\x1a\n" + "�" * 200 + "IHDR")
    assert looks_binary("abc\x00def")
    assert not looks_binary("def hello():\n    return 'world'\n" * 50)
    assert not looks_binary("")


def test_index_file_refuses_images_and_binary_content(tmp_path):
    indexer = ProjectIndexer(vector_store=None, keyword_store=None, embedding_service=None)
    png = tmp_path / "shot.png"
    png.write_bytes(b"\x89PNG" + bytes(range(256)) * 20)
    assert indexer.index_file(png, project_id="p") == []

    disguised = tmp_path / "notes.txt"
    disguised.write_bytes(bytes(range(256)) * 20)
    assert indexer.index_file(disguised, project_id="p") == []


def test_token_estimate_charges_non_ascii_per_character():
    counter = TokenCounter(chars_per_token=4.0)
    assert counter.count("a" * 400) == 100
    assert counter.count("�" * 400) == 400
    assert counter.count("a" * 400 + "中" * 10) == 110


def test_retrieval_drops_binary_looking_chunks():
    cm = ContextManager.__new__(ContextManager)
    junk = {"content": "��\x01\x02" * 100, "file_name": "x.png"}
    code = {"content": "def f():\n    return 1\n" * 20, "file_name": "x.py"}
    assert cm._chunk_looks_binary(junk)
    assert not cm._chunk_looks_binary(code)
