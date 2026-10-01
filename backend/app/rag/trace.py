"""
RAG trace: what retrieval found for a workspace question, stage by stage, and what the model
answered from it (PLAN §4.8a step 1, 2026-10-01).

The user's model gets names wrong when answering from their reference document. Before any fix
the question is *where* the right name is lost: never indexed, not found by the semantic or the
keyword search, cut by the reranker's floor or its "flat relevance" rule, dropped by the token
budget -- or delivered to the model, which then wrote something else. A trace records each of
those stages for one query, so a failing answer can be read back next to the exact passages the
model had.

Live turns append one JSON line per WORKSPACE turn to ``TRACE_PATH`` (never for off-the-record
turns: those keep nothing). ``POST /api/rag/trace`` runs the same retrieval for any question and
returns the trace without asking the model; ``scripts/rag_name_check.py`` uses it.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from app.config import BASE_DIR

logger = logging.getLogger("jarvis.rag.trace")

TRACE_PATH = Path(os.environ.get("RAG_TRACE_PATH") or (BASE_DIR / "data" / "rag_trace.jsonl"))
MAX_BYTES = 10 * 1024 * 1024  # then the file moves to .1 and a new one starts
PREVIEW_CHARS = 160

_lock = threading.Lock()


def chunk_id(chunk: dict[str, Any]) -> str:
    return str(chunk.get("chunk_id") or chunk.get("id") or "")


def chunk_text(chunk: dict[str, Any]) -> str:
    return str(chunk.get("content") or chunk.get("text") or "")


def brief(chunk: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """One candidate as the trace keeps it: where it is from, its scores, the start of its text."""
    out: dict[str, Any] = {
        "id": chunk_id(chunk),
        "file": chunk.get("file_path") or chunk.get("source") or (chunk.get("metadata") or {}).get("file_path"),
    }
    for key in ("start_line", "end_line"):
        if chunk.get(key) is not None:
            out[key] = chunk[key]
    for key in ("similarity_score", "keyword_score", "semantic_score", "lexical_score", "score", "rrf_score"):
        if chunk.get(key) is not None:
            out[key] = round(float(chunk[key]), 4)
    out["preview"] = " ".join(chunk_text(chunk).split())[:PREVIEW_CHARS]
    out.update(extra)
    return out


# Words that start sentences or are capitalised for other reasons; not names.
_NOT_NAMES = frozenset("""
I A An The This That These Those It Its He She They We You Your My Our His Her Their There Here
What Which Who Whom Whose When Where Why How Yes No Not And But Or So If Then Also However In On At
For From To Of By With As Is Are Was Were Be Been Being Do Does Did Has Have Had Can Could Would
Should Will May Might Must Sure Okay Ok Based According Document Text Chapter Section Note
""".split())
_CAPITALISED_RE = re.compile(r"\b[A-Z][a-zA-Z'\-]+(?:\s+[A-Z][a-zA-Z'\-]+)*")


def names_not_in(answer: str, chunks: Iterable[dict[str, Any]]) -> list[str]:
    """Capitalised words and phrases in the answer that appear in none of the passages the model
    was given -- a rough flag for names it made up (or took from elsewhere: memory, history,
    general knowledge). A list for a person to read, not a verdict."""
    haystack = " ".join(chunk_text(c) for c in chunks).lower()
    seen: list[str] = []
    for m in _CAPITALISED_RE.finditer(answer or ""):
        words = [w for w in m.group(0).split() if w not in _NOT_NAMES]
        if not words:
            continue
        phrase = " ".join(words)
        if phrase.lower() not in haystack and phrase not in seen:
            seen.append(phrase)
    return seen


def write(record: dict[str, Any]) -> None:
    """Append one trace as a JSON line; never lets a write problem reach the turn."""
    record = {"at": datetime.now().isoformat(timespec="seconds"), **record}
    try:
        with _lock:
            TRACE_PATH.parent.mkdir(parents=True, exist_ok=True)
            if TRACE_PATH.exists() and TRACE_PATH.stat().st_size > MAX_BYTES:
                TRACE_PATH.replace(TRACE_PATH.with_suffix(".jsonl.1"))
            with TRACE_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("Could not write the RAG trace: %s", e)


def finish(
    trace: dict[str, Any],
    used: list[dict[str, Any]],
    dropped: list[dict[str, Any]],
    answer: Optional[str] = None,
    tools_used: Optional[list[Any]] = None,
) -> dict[str, Any]:
    """Add what the context budget kept and, for a live turn, the answer and its unsupported
    names. The full text of every passage the model got is kept: that is the evidence."""
    trace["sent_to_model"] = [{"id": chunk_id(c), "text": chunk_text(c)} for c in used]
    trace["dropped_by_budget"] = [brief(c) for c in dropped]
    if answer is not None:
        trace["answer"] = answer
        trace["names_not_in_passages"] = names_not_in(answer, used)
        trace["tools_used"] = [t.get("tool") if isinstance(t, dict) else t for t in (tools_used or [])]
    return trace


def locate(trace: dict[str, Any], holding: list[dict[str, Any]]) -> dict[str, Any]:
    """Where an expected answer (a name) was lost: ``holding`` is every indexed passage that
    contains it (``KeywordSearchService.chunks_containing``). Stage by stage, does any of them
    survive? The first stage it doesn't is the verdict."""
    ids = {chunk_id(c) for c in holding}

    def found(stage: str) -> bool:
        return any(c.get("id") in ids for c in trace.get(stage) or [])

    stages = {
        "indexed": bool(ids),
        "semantic_top": found("semantic"),
        "keyword_top": found("keyword"),
        "reranked": found("reranked"),
        "final": found("final"),
    }
    if not stages["indexed"]:
        verdict = "not indexed: no passage in this workspace's index contains it"
    elif stages["final"]:
        verdict = "retrieved: a passage with it reaches the model -- a wrong answer is a generation problem"
    elif trace.get("stopped") and not (stages["semantic_top"] or stages["keyword_top"] or stages["reranked"]):
        verdict = f"retrieval stopped early: {trace['stopped']}"
    elif not (stages["semantic_top"] or stages["keyword_top"]):
        verdict = "neither the semantic nor the keyword search ranked a passage with it in its top 20"
    elif not stages["reranked"]:
        verdict = "found by a search but outside the candidates the reranker scored"
    elif trace.get("stopped"):
        verdict = f"cut by the reranker: {trace['stopped']}"
    else:
        floor = trace.get("floor")
        verdict = (f"cut by the reranker: below the relevance floor {floor} or past top {trace.get('top_k')}"
                   if floor is not None else f"cut by the reranker: past top {trace.get('top_k')}")
    return {"stages": stages, "verdict": verdict,
            "passages": [brief(c) for c in holding[:10]]}
