---
name: focus_timer
description: Pomodoro-style focus sessions with reminders for the work and break times.
triggers:
  - pomodoro
  - focus timer
  - focus session
  - start a timer
  - study session
tools:
  - remind_me
---

## Focus timer instructions
Default 25 minutes of work, 5 minutes of break; after four rounds, a 15 minute break. The user can name other lengths.
- Starting: ask what they are working on only if they didn't say. Set the end of the work block with `remind_me` ("Focus block done: take 5") in `in N minutes`, repeat once. Say when it will ring.
- Set only the next reminder, and the next after the user confirms they are starting the break or the next round; don't queue a chain of reminders.
- Stopping early: reminders can't be cancelled by this skill; say so and let the user cancel it from the reminders list (or tell them it will still ring).
- Keep replies to a line or two; don't narrate the technique.
