---
name: flashcards
description: Make flashcards from notes or a topic and quiz the user with spaced repetition.
triggers:
  - flashcard
  - flash card
  - quiz me
  - spaced repetition
  - revision card
  - anki
tools:
  - read_file
  - write_file
  - remind_me
---

## Flashcard instructions
Keep one file per subject: `flashcards/<subject>.csv` in the workspace with columns `question,answer,box,due` (box 1-5, due = YYYY-MM-DD). `read_file` it before changing it and `write_file` the whole file back.
- Making cards: from the user's notes (read_file them) or a topic; short questions with one fact each, new cards box 1, due today. Show the cards before saving if there are more than 10.
- Quizzing: pick the cards due today or earlier (read the file, check today's date from the conversation), ask one at a time, wait for the answer, then say right or wrong and show the answer.
- Scoring: right -> box + 1 (max 5); wrong -> box 1. Next due: box 1 = tomorrow, 2 = 3 days, 3 = 7 days, 4 = 14 days, 5 = 30 days. Save once at the end of the session, not after each card.
- Offer `remind_me` for the next due date when asked; don't set one unprompted.
