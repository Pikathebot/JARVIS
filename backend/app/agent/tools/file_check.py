"""
Check a file right after write_file / patch_file changed it (PLAN 4.15 #5, after harness2: "a
successful write call does not establish usability"). The file is read back from disk and, for
formats with a parser at hand, parsed; the result is one short sentence appended to the tool's
own result, so the model can fix a broken file in the same turn.

It runs inside the tool, on the path the tool already resolved: it is never a model tool call and
never a taint source, and a write replayed after an approval (paused_turns) is checked the same way.
A failure inside the checker says nothing rather than turning a good write into an error.
"""
import csv
import io
import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("jarvis.agent.tools.file_check")

MAX_CHECK_BYTES = 2_000_000  # bigger files are written but not parsed


def _json(text: str) -> str:
    try:
        json.loads(text)
    except json.JSONDecodeError as e:
        return f"Warning: this is not valid JSON ({e.msg} at line {e.lineno}, column {e.colno}); fix it."
    return "Checked: valid JSON."


def _jsonl(text: str) -> str:
    for n, line in enumerate(text.splitlines(), 1):
        if line.strip():
            try:
                json.loads(line)
            except json.JSONDecodeError as e:
                return f"Warning: line {n} is not valid JSON ({e.msg}); fix it."
    return "Checked: every line is valid JSON."


def _python(text: str, name: str) -> str:
    try:
        compile(text, name, "exec")
    except SyntaxError as e:
        return f"Warning: Python syntax error at line {e.lineno}: {e.msg}; fix it."
    return "Checked: the Python compiles."


def _csv(text: str) -> str:
    rows = [r for r in csv.reader(io.StringIO(text)) if r]
    if len(rows) < 2:
        return ""
    width = len(rows[0])
    for n, row in enumerate(rows[1:], 2):
        if len(row) != width:
            return f"Warning: row {n} has {len(row)} columns but the header has {width}; check the quoting."
    return f"Checked: {len(rows)} rows, {width} columns each."


def _toml(text: str) -> str:
    import tomllib
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        return f"Warning: this is not valid TOML ({e}); fix it."
    return "Checked: valid TOML."


def _yaml(text: str) -> str:
    try:
        import yaml
    except ImportError:
        return ""
    try:
        yaml.safe_load(text)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" at line {mark.line + 1}" if mark is not None else ""
        return f"Warning: this is not valid YAML{where}; fix it."
    return "Checked: valid YAML."


def check_written(path: Path, expected: Optional[str] = None) -> str:
    """One sentence about the file as it now is on disk, or "" when there is nothing to say."""
    try:
        path = Path(path)
        if path.stat().st_size > MAX_CHECK_BYTES:
            return ""
        raw = path.read_bytes().decode("utf-8")
        # Line endings are the platform's business (text mode writes \r\n on Windows): compare without them.
        if expected is not None and raw.replace("\r", "") != expected.replace("\r", ""):
            return (f"Warning: the file on disk ({len(raw)} chars) doesn't match what was written "
                    f"({len(expected)} chars); read it back before saying it is saved.")
        text = raw.replace("\r\n", "\n")
        suffix = path.suffix.lower()
        if suffix in (".json", ".ipynb"):
            return _json(text)
        if suffix == ".jsonl":
            return _jsonl(text)
        if suffix in (".py", ".pyw"):
            return _python(text, path.name)
        if suffix == ".csv":
            return _csv(text)
        if suffix == ".toml":
            return _toml(text)
        if suffix in (".yaml", ".yml"):
            return _yaml(text)
        return ""
    except Exception as e:  # never turn a good write into an error
        logger.debug("No check for '%s': %s", path, e)
        return ""
