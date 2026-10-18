"""Failure kinds read from real tool error text (PLAN 4.15 #4)."""
import pytest

from app.agent.tool_errors import NEXT_STEP, error_kind


@pytest.mark.parametrize("result, kind", [
    ("Error: tool 'fetch_url' timed out after 30 seconds. It may still be running", "timeout"),
    ("Error: Request timed out while attempting to load 'https://x'.", "timeout"),
    ("Error: not available here: this workspace is not a git repository. Don't retry.", "unavailable"),
    ("Error: 'delete_file' works on project files, and this is a Freeform conversation", "unavailable"),
    ("Error: 'https://reddit.com/r/x' shows a sign-in page to visitors who aren't logged in", "unavailable"),
    ("Error: Access denied. 'C:/x' resolves outside the workspace.", "not_permitted"),
    ("Error: Windows refused to lock.", "not_permitted"),
    ("Validation Error for read_file: field required", None),  # stream loop's text, not a tool's
    ("Error: File path cannot be empty.", "bad_input"),
    ("Error: 'docs' is a directory, not a file.", "bad_input"),
    ("Error: Unknown media action 'resume'. Valid actions are: play, pause.", "bad_input"),
    ("Error: The search block was not found in 'a.py'. The closest part of the file is:", "bad_input"),
    ("Error: File not found at 'x.md' within workspace 'D:/w'.", "not_found"),
    ("Error: 'notes.txt' does not exist.", "not_found"),
    ("Error: No running processes found matching 'spotify'.", "not_found"),
    ("Error: HTTP 404 (Not Found) when fetching 'https://x'", "not_found"),
    ("Error executing tool 'x': boom", "other"),
    ("Successfully wrote 10 characters", None),
    ("Found 3 match(es) ... Error: in the middle doesn't count", None),
])
def test_error_kinds(result, kind):
    assert error_kind(result) == kind


def test_every_kind_has_a_next_step_entry():
    kinds = {"timeout", "unavailable", "not_permitted", "bad_input", "not_found", "other"}
    assert kinds == set(NEXT_STEP)
    assert all("again" not in line.lower() or "don't" in line.lower() or "once more" in line
               for line in NEXT_STEP.values())
