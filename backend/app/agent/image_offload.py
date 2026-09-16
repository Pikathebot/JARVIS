"""
Image offload: when a request with images does not fit, send it again without them.

Image tokens are decided by the projector at encode time (Qwen3.5 uses dynamic resolution),
so the context manager can only reserve a guess per image. When the guess is low, llama-server
rejects the whole request for exceeding the context, and without this the turn simply fails.
Replacing each image part with a short placeholder and retrying once keeps the turn alive:
the model loses the pictures but still answers, and says why it could not see them.
"""
from __future__ import annotations

import re
from typing import Any

PLACEHOLDER = "[image omitted: it did not fit in the model's context window]"

_OVERFLOW_MARKERS = (
    "exceeds the available context",
    "exceed the available context",
    "context size",
    "n_ctx",
    "too many tokens",
    "input is too large",
    "image",
)


def looks_like_context_overflow(status_code: int, body: str) -> bool:
    """A 4xx/5xx from llama-server whose text says the prompt or its images did not fit."""
    if status_code == 200:
        return False
    text = (body or "").lower()
    return any(marker in text for marker in _OVERFLOW_MARKERS)


def has_image_parts(messages: list[dict[str, Any]]) -> bool:
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list) and any(
            isinstance(p, dict) and p.get("type") == "image_url" for p in content
        ):
            return True
    return False


def offload_images(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """
    Copy of ``messages`` with every ``image_url`` part replaced by a text placeholder, and how
    many were replaced. A message left with only text parts is flattened back to a string.
    """
    replaced = 0
    out: list[dict[str, Any]] = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            out.append(msg)
            continue
        texts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                texts.append(PLACEHOLDER)
                replaced += 1
            elif isinstance(part, dict) and part.get("type") == "text":
                texts.append(str(part.get("text") or ""))
            else:
                texts.append(str(part))
        out.append({**msg, "content": "\n".join(t for t in texts if t)})
    return out, replaced
