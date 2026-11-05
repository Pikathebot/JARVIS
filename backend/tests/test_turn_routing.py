"""PLAN 4.4: the per-message keyword gates (skill triggers, the toolless fast model) measured on the eval prompts."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import check_turn_routing  # noqa: E402

from app.skills.loader import trigger_in  # noqa: E402


def test_every_eval_prompt_and_paraphrase_meets_its_label():
    rows = check_turn_routing.measure()
    assert len(rows) > 100
    assert check_turn_routing.problems(rows) == []


@pytest.mark.parametrize("trigger, text, hit", [
    ("uat", "evaluating retrieval augmented generation", False),
    ("ubt", "i have a doubt", False),
    ("uat", "run uat on the build", True),
    ("flashcard", "make flashcards from os_notes.md", True),
    ("flash card", "two flash  cards please", True),
    ("c++ reflection", "how does c++ reflection work in ue5", True),
    ("quiz me", "quiz meat", False),
    ("", "anything", False),
])
def test_triggers_match_whole_words(trigger, text, hit):
    assert trigger_in(trigger, text) is hit
