"""
Hands-free voice session state.

The desktop client owns the microphone; this module owns the decision of what
a captured utterance *means*. Keeping that decision on the server means the
wake words, the follow-up window and the barge-in rules have exactly one
definition, shared by every client.

The state machine per session:

    IDLE ──start──> LISTENING ──wake word──> ARMED ──utterance──> THINKING
      ^                 ^                      │                     │
      │                 └──follow-up window────┘                     │
      └──────────────────── stop ─── SPEAKING <─────────────────────-┘

ARMED is what makes it feel like Jarvis rather than a walkie-talkie: for a
window after a reply, follow-up questions need no wake word. The window grows
while the user keeps talking (15 s, then 45 s, then 90 s by default) and drops
back to the first rung once one lapses: a conversation needs the wake word
once, coming back after a long quiet needs it again. (The user's rule,
2026-10-13; on device a reply 52 s into a quick back-and-forth was ignored.)
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from app.voice.wake_word import WakeWordDetector

logger = logging.getLogger("jarvis.voice.session")


class VoiceState(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"
    ARMED = "armed"
    THINKING = "thinking"
    SPEAKING = "speaking"


@dataclass
class VoiceSession:
    session_id: str
    state: VoiceState = VoiceState.IDLE
    armed_until: float = 0.0
    last_wake_word: Optional[str] = None
    last_transcript: str = ""
    turns: int = 0
    streak: int = 0  # follow-ups in a row without the window lapsing: picks the window's rung
    updated_at: float = field(default_factory=time.time)

    def is_armed(self, now: Optional[float] = None) -> bool:
        return (now if now is not None else time.time()) < self.armed_until

    def to_dict(self, now: Optional[float] = None) -> dict[str, Any]:
        current = now if now is not None else time.time()
        return {
            "session_id": self.session_id,
            "state": self.state.value,
            "armed": self.is_armed(current),
            "armed_seconds_remaining": max(0.0, round(self.armed_until - current, 1)),
            "last_wake_word": self.last_wake_word,
            "last_transcript": self.last_transcript,
            "turns": self.turns,
            "streak": self.streak,
        }


@dataclass
class UtteranceDecision:
    """What the server decided to do with one captured utterance."""

    should_respond: bool
    query: str = ""
    transcript: str = ""
    wake_detected: bool = False
    wake_word: Optional[str] = None
    reason: str = ""
    # A canned reply to speak without involving the agent (e.g. the greeting
    # after a bare "Jarvis" with no command attached).
    speak_immediately: str = ""
    state: VoiceState = VoiceState.LISTENING

    def to_dict(self) -> dict[str, Any]:
        return {
            "should_respond": self.should_respond,
            "query": self.query,
            "transcript": self.transcript,
            "wake_detected": self.wake_detected,
            "wake_word": self.wake_word,
            "reason": self.reason,
            "speak_immediately": self.speak_immediately,
            "state": self.state.value,
        }


class VoiceSessionManager:
    """Tracks hands-free state for each chat session."""

    def __init__(
        self,
        detector: Optional[WakeWordDetector] = None,
        follow_up_window_seconds: float = 15.0,
        min_transcript_chars: int = 2,
        follow_up_ladder: Optional[tuple[float, ...]] = None,
    ):
        self.detector = detector or WakeWordDetector()
        self.follow_up_window_seconds = follow_up_window_seconds
        # Rung n is the window after n follow-ups in a row; the last rung is the ceiling.
        self.follow_up_ladder = follow_up_ladder or (
            follow_up_window_seconds, follow_up_window_seconds * 3, follow_up_window_seconds * 6
        )
        self.min_transcript_chars = min_transcript_chars
        self._sessions: dict[str, VoiceSession] = {}

    # ------------------------------------------------------------- sessions

    def get(self, session_id: str) -> VoiceSession:
        key = session_id or "default"
        if key not in self._sessions:
            self._sessions[key] = VoiceSession(session_id=key)
        return self._sessions[key]

    def reset(self, session_id: str) -> VoiceSession:
        key = session_id or "default"
        self._sessions[key] = VoiceSession(session_id=key)
        return self._sessions[key]

    def start_listening(self, session_id: str) -> VoiceSession:
        session = self.get(session_id)
        session.state = VoiceState.LISTENING
        session.updated_at = time.time()
        return session

    def stop_listening(self, session_id: str) -> VoiceSession:
        session = self.get(session_id)
        session.state = VoiceState.IDLE
        session.armed_until = 0.0
        session.updated_at = time.time()
        return session

    def set_state(self, session_id: str, state: VoiceState) -> VoiceSession:
        session = self.get(session_id)
        session.state = state
        session.updated_at = time.time()
        if state in (VoiceState.IDLE,):
            session.armed_until = 0.0
        return session

    def arm_follow_up(self, session_id: str, window_seconds: Optional[float] = None) -> VoiceSession:
        """
        After a reply, stay armed so the next question needs no wake word -- longer the more
        the user has been talking (see the module docstring).
        """
        session = self.get(session_id)
        if window_seconds is None:
            window = self.follow_up_ladder[min(session.streak, len(self.follow_up_ladder) - 1)]
        else:
            window = window_seconds
        session.armed_until = time.time() + max(0.0, window)
        session.state = VoiceState.LISTENING
        session.updated_at = time.time()
        return session

    # ------------------------------------------------------------ decisions

    def evaluate_utterance(
        self,
        session_id: str,
        transcript: str,
        greeting: str = "",
        spoken_seconds: float = 0.0,
    ) -> UtteranceDecision:
        """
        Decide whether a transcribed utterance is addressed to Jarvis.

        ``spoken_seconds`` is how long the utterance lasted: the follow-up window is checked
        against when the user *started* talking, so a sentence begun inside it counts.

        Returns a decision rather than acting, so the caller stays in control
        of dispatching to the agent.
        """
        session = self.get(session_id)
        now = time.time()
        started = now - max(0.0, spoken_seconds or 0.0)
        clean = (transcript or "").strip()

        if len(clean) < self.min_transcript_chars:
            return UtteranceDecision(
                should_respond=False,
                transcript=clean,
                reason="empty_transcript",
                state=session.state,
            )

        session.last_transcript = clean
        detected, wake_word, remainder = self.detector.detect_in_text(clean)
        in_window = session.is_armed(started)

        # A wake word always takes priority: it re-addresses Jarvis explicitly.
        if detected:
            session.last_wake_word = wake_word
            if remainder:
                session.state = VoiceState.THINKING
                session.turns += 1
                # Said inside the window it's the same conversation; after a lapse, a new one.
                session.streak = session.streak + 1 if in_window else 0
                session.armed_until = 0.0
                return UtteranceDecision(
                    should_respond=True,
                    query=remainder,
                    transcript=clean,
                    wake_detected=True,
                    wake_word=wake_word,
                    reason="wake_word_with_command",
                    state=session.state,
                )

            # Bare "Jarvis" - acknowledge and wait for the actual request.
            session.state = VoiceState.ARMED
            if not in_window:
                session.streak = 0
            session.armed_until = now + self.follow_up_window_seconds
            return UtteranceDecision(
                should_respond=False,
                transcript=clean,
                wake_detected=True,
                wake_word=wake_word,
                reason="wake_word_awaiting_command",
                speak_immediately=greeting,
                state=session.state,
            )

        # No wake word, but we are still within the follow-up window.
        if in_window:
            session.state = VoiceState.THINKING
            session.turns += 1
            session.streak += 1
            session.armed_until = 0.0
            return UtteranceDecision(
                should_respond=True,
                query=clean,
                transcript=clean,
                reason="follow_up_window",
                state=session.state,
            )

        if session.state not in (VoiceState.THINKING, VoiceState.SPEAKING):
            session.streak = 0  # the window lapsed: the conversation is over
        return UtteranceDecision(
            should_respond=False,
            transcript=clean,
            reason="no_wake_word",
            state=session.state,
        )

    def status(self) -> dict[str, Any]:
        now = time.time()
        return {
            "wake_words": list(self.detector.wake_words),
            "follow_up_window_seconds": self.follow_up_window_seconds,
            "sessions": [s.to_dict(now) for s in self._sessions.values()],
        }
