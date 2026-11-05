"""scripts/cloud/setup.sh (the cloud environment's setup script) pins every package backend/requirements.txt needs on Linux."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WINDOWS_ONLY = re.compile(r"^(pywin32|winotify|winrt-.*)$")


def _name(spec: str) -> str:
    return re.split(r"[<>=!~\[; ]", spec.strip(), maxsplit=1)[0].lower().replace("_", "-")


def _pins() -> dict:
    text = (ROOT / "scripts" / "cloud" / "setup.sh").read_text(encoding="utf-8")
    block = text.split("<<'PINS'\n", 1)[1].split("\nPINS\n", 1)[0]
    return {_name(line): line.split("==", 1)[1] for line in block.splitlines() if line.strip()}


def test_every_linux_requirement_is_pinned_exactly():
    pins = _pins()
    assert len(pins) > 50 and all(re.fullmatch(r"[\w.+-]+", v) for v in pins.values())
    wanted = {_name(l) for l in (ROOT / "backend" / "requirements.txt").read_text(encoding="utf-8").splitlines()
              if l.split("#")[0].strip()}
    missing = sorted(n for n in wanted if not WINDOWS_ONLY.match(n) and n not in pins)
    assert missing == [], f"add these to the PINS list in scripts/cloud/setup.sh: {missing}"
    assert not any(WINDOWS_ONLY.match(n) for n in pins)


def test_the_script_never_fails_the_session_start():
    text = (ROOT / "scripts" / "cloud" / "setup.sh").read_text(encoding="utf-8")
    assert "set -e" not in text and text.rstrip().endswith("exit 0")
    assert "rm -rf /opt/jarvis-venv" in text and "rm -rf $" not in text and 'rm -rf "$' not in text
