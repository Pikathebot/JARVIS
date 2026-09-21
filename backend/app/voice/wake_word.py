import logging
import re
from typing import Callable, Optional

logger = logging.getLogger("jarvis.voice.wake_word")

DEFAULT_WAKE_WORDS = (
    "jarvis",
    "hey jarvis",
    "ok jarvis",
    "okay jarvis",
    "hello jarvis",
)

# What a small Whisper model actually writes when someone says "Jarvis" into a laptop mic. The
# name is not in tiny.en's vocabulary, so it lands on the nearest English-looking sound. Each
# alias is a whole word; the detector rewrites it to the canonical spelling before matching so
# the wake words above stay the single source of truth.
WAKE_WORD_ALIASES = (
    "jarvis", "jarves", "jarvus", "jarvess", "jarviss", "jarvis'", "jarvies",
    "jarbis", "jarves", "jarmes", "jarmis", "jervis", "jervas", "javis", "javas",
    "jarvas", "jarvic", "charvis", "garvis", "jarvish",
)


class WakeWordDetector:
    """
    Evaluates audio transcriptions or text streams for Jarvis wake words
    and triggers callback actions.
    """

    def __init__(
        self,
        wake_words: tuple[str, ...] = DEFAULT_WAKE_WORDS,
        on_wake: Optional[Callable[[str], None]] = None
    ):
        self.wake_words = [w.lower().strip() for w in wake_words]
        self.on_wake = on_wake
        self._is_listening = False
        self._compile_patterns()

    def _compile_patterns(self) -> None:
        patterns = [re.escape(w) for w in self.wake_words]
        self._regex = re.compile(rf"\b({'|'.join(patterns)})\b", re.IGNORECASE)
        aliases = sorted({a for a in WAKE_WORD_ALIASES if a != "jarvis"}, key=len, reverse=True)
        self._alias_regex = re.compile(
            rf"\b({'|'.join(re.escape(a) for a in aliases)})\b", re.IGNORECASE
        )

    def normalize(self, text: str) -> str:
        """Rewrite STT mishearings of the name ("Jarmes", "Jervis") to "Jarvis"."""
        return self._alias_regex.sub("Jarvis", text)

    def detect_in_text(self, text: str) -> tuple[bool, Optional[str], str]:
        """
        Check if text contains a wake word.
        Returns: (detected, matched_wake_word, remaining_query_text)
        """
        text = self.normalize(text)
        match = self._regex.search(text)
        if not match:
            return False, None, text

        matched_word = match.group(0)
        # Extract query text following the wake word
        remaining = text[match.end():].strip().lstrip(",.?! ")
        
        logger.info("Wake word '%s' detected. Extracted query: '%s'", matched_word, remaining)
        if self.on_wake:
            self.on_wake(remaining)

        return True, matched_word, remaining

    def start_listening(self) -> None:
        self._is_listening = True
        logger.info("Wake-word listener started.")

    def stop_listening(self) -> None:
        self._is_listening = False
        logger.info("Wake-word listener stopped.")

    @property
    def is_listening(self) -> bool:
        return self._is_listening
