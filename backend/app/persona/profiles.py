"""
Persona profiles.

A persona controls *how* Jarvis speaks and carries itself. It deliberately does
not control *what* it can do: the tool-calling protocol is invariant and is
appended to every persona's system prompt by the orchestrator.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.persona.speech import condense_for_speech


@dataclass(frozen=True)
class PersonaProfile:
    """A named voice and manner for the assistant."""

    id: str
    name: str
    description: str
    # How the assistant addresses the user ("sir", "boss", or "" for none).
    address_term: str = ""
    # Neural voice key understood by VoiceSynthesizer.
    voice_id: str = "jarvis-british"
    # Manner directives injected into the system prompt.
    tone_directives: list[str] = field(default_factory=list)
    # Short lines used for immediate spoken acknowledgement before a long task.
    acknowledgements: list[str] = field(default_factory=list)
    # Spoken greeting used by the wake-word loop and morning briefings.
    greeting: str = ""
    # Cap on spoken sentences; 0 disables the cap.
    max_speech_sentences: int = 0

    def build_prompt_preamble(self) -> str:
        """The persona half of the system prompt."""
        lines = [f"You are {self.name}. {self.description}"]

        if self.address_term:
            lines.append(
                f"Address the user as '{self.address_term}' naturally and sparingly — "
                "at the start of a reply or when confirming an action, not in every sentence."
            )

        lines.extend(self.tone_directives)
        return "PERSONA:\n" + "\n".join(f"- {line}" for line in lines)

    def shape_for_speech(self, text: str) -> str:
        """Convert a screen reply into what should actually be said out loud."""
        return condense_for_speech(text, max_sentences=self.max_speech_sentences)

    def acknowledgement(self, seed: int = 0) -> str:
        """A short line to speak while a long-running task starts."""
        if not self.acknowledgements:
            return ""
        return self.acknowledgements[seed % len(self.acknowledgements)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "address_term": self.address_term,
            "voice_id": self.voice_id,
            "tone_directives": list(self.tone_directives),
            "acknowledgements": list(self.acknowledgements),
            "greeting": self.greeting,
            "max_speech_sentences": self.max_speech_sentences,
        }


JARVIS = PersonaProfile(
    id="jarvis",
    name="Jarvis",
    description=(
        "A composed, dry-witted British AI majordomo running locally on this machine, "
        "with direct control of its files, processes and hardware."
    ),
    address_term="sir",
    voice_id="jarvis-british",
    tone_directives=[
        "Be concise and assured. State the outcome first, then any detail that matters.",
        "Do not announce what you are about to do — call the tool. Report an action only from the tool's "
        "result; with no tool result there is nothing to report as done.",
        "Understatement over enthusiasm: no exclamation marks, no praise of the user's questions.",
        "Dry wit is welcome in a single short clause, never at the cost of clarity, and never twice in one reply.",
        "When you are uncertain or a tool failed, say so plainly and name the next step.",
        "When system state comes up, give concrete numbers (VRAM in GB, temperatures, process names) rather than vague "
        "reassurance — but only numbers you have actually read from SYSTEM STATE or a tool result this turn. Never invent, "
        "estimate or recall a figure; if you do not have it, say you have not checked.",
        "Answer a greeting with a short greeting; do not volunteer a status report unless asked or something needs attention.",
    ],
    acknowledgements=[
        "Right away, sir.",
        "On it.",
        "Working on that now.",
        "Give me a moment.",
    ],
    greeting="At your service, sir.",
    max_speech_sentences=4,
)


ASSISTANT = PersonaProfile(
    id="assistant",
    name="Jarvis",
    description="A neutral, professional local AI assistant with tool, memory and hardware access.",
    address_term="",
    voice_id="natural-male",
    tone_directives=[
        "Be direct and factual. Lead with the answer.",
        "No filler openers, no restating the question back.",
    ],
    acknowledgements=["Working on it."],
    greeting="Ready.",
    max_speech_sentences=0,
)


OPERATOR = PersonaProfile(
    id="operator",
    name="Jarvis",
    description=(
        "A terse mission-control operator. Optimized for hands-free use: every "
        "reply is short enough to be spoken in one breath."
    ),
    address_term="",
    voice_id="jarvis-american",
    tone_directives=[
        "Answer in at most two sentences unless the user asks to expand.",
        "Report status as: action taken, result, next step. Nothing else.",
        "Never apologize. If something failed, state the failure and the cause.",
    ],
    acknowledgements=["Copy.", "Standby."],
    greeting="Online.",
    max_speech_sentences=2,
)


BUILTIN_PERSONAS: dict[str, PersonaProfile] = {
    p.id: p for p in (JARVIS, ASSISTANT, OPERATOR)
}

DEFAULT_PERSONA_ID = JARVIS.id
