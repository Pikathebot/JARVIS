import math
import logging
from typing import Any, Callable, Optional

logger = logging.getLogger("jarvis.memory.token_counter")


class TokenCounter:
    """
    Token Counter Service with sub-millisecond heuristic estimation and optional exact tokenizer hook.
    Guarantees reliable token budgeting for KV cache overflow protection.
    """

    def __init__(
        self,
        chars_per_token: float = 3.8,
        custom_counter: Optional[Callable[[str], int]] = None
    ):
        self.chars_per_token = max(1.0, chars_per_token)
        self._custom_counter = custom_counter

    def count(self, text: str) -> int:
        """
        Count tokens in a string.
        Uses exact custom counter if provided, otherwise calculates using calibrated character ratio.
        """
        if not text:
            return 0

        if self._custom_counter is not None:
            try:
                return self._custom_counter(text)
            except Exception as e:
                logger.debug("Custom tokenizer error (%s), falling back to ratio heuristic.", e)

        # 1 token ≈ 3.8 characters holds for code and English. Anything outside ASCII --
        # CJK, emoji, and above all the U+FFFD soup of a binary file read as text -- tokenizes
        # at roughly one token per character, and pricing it at 3.8 let an 11k-token prompt
        # through an 8k budget.
        non_ascii = sum(1 for ch in text if ord(ch) > 127)
        ascii_chars = len(text) - non_ascii
        return max(1, math.ceil(ascii_chars / self.chars_per_token) + non_ascii)

    def count_messages(self, messages: list[dict[str, Any]]) -> int:
        """
        Count total tokens across a list of chat messages including role wrappers and delimiters.
        """
        if not messages:
            return 0

        total = 0
        for msg in messages:
            # Per-message format overhead (<|im_start|>role\n ... <|im_end|>\n ≈ 4 tokens)
            total += 4
            content = msg.get("content")
            if isinstance(content, str):
                total += self.count(content)
            elif isinstance(content, list):
                # Multimodal or multi-part content
                for part in content:
                    if isinstance(part, dict) and "text" in part:
                        total += self.count(part["text"])

            # Count tool calls if present
            tool_calls = msg.get("tool_calls")
            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    total += 6  # wrapper overhead
                    fn = tc.get("function", {})
                    fn_name = fn.get("name", "")
                    fn_args = fn.get("arguments", "")
                    total += self.count(fn_name)
                    if isinstance(fn_args, str):
                        total += self.count(fn_args)
                    elif isinstance(fn_args, dict):
                        import json
                        total += self.count(json.dumps(fn_args))

        # Base prompt framing overhead
        total += 3
        return total
