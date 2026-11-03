"""Turn what Jarvis has collected into fine-tune data for Ornith (PLAN 4.13 #10, step 2 of the agreed order).

Reads (all local, git-ignored):
- ``data/preference_votes.jsonl``: the user's votes, from the review page (``review_answers.py``) and the in-app compare card.
- ``data/training_log.jsonl``: turns where a safety net fired (``net``), the memory / PROJECT.md capture (``capture``), and
  replies the user flagged "got it wrong" (``wrong``, withdrawn by ``unflagged``).

Writes to ``data/finetune/`` (git-ignored, under ``/data/``):
- ``dpo.jsonl``: training-ready preference pairs in the conversational form TRL-style trainers (Soup included) take:
  ``{"prompt": [{"role": "user", ...}], "chosen": [{"role": "assistant", ...}], "rejected": [...], "source", "id"}``.
  An in-app vote carries the chat turns before it (``compare.earlier_turns``), which become the start of ``prompt``; one saved
  before that was stored is kept with ``context_stored: false`` (counted in the manifest -- filter them out if they look wrong).
  A multi-turn review-page vote is dropped: the page stores the earlier *user* turns but not the answers between them. Ties, empty or identical sides, and votes whose chosen answer failed the
  eval's check while the rejected one passed are dropped. A later vote on the same pair replaces an earlier one.
- ``review.jsonl``: rows that need a person (or a teacher model) before they can train anything -- each net-fired turn
  (the draft that was sent back, the note, the tools, the reply that was kept) and each flagged reply with what the user
  meant. They are not pairs yet: a kept reply was written after tools ran, so pairing it with the draft against the user's
  message alone would teach the model to answer without the tool results.
- ``manifest.json``: counts, what was dropped and why, and how many redactions were made.

Personal details are redacted on the way out (the training log keeps them; this is the step before anything leaves the
PC): e-mail addresses, phone numbers, ``C:\\Users\\<name>`` paths, and any words given with ``--names`` (case-insensitive,
whole words). Read ``review.jsonl`` before uploading anything; the redaction is a filter, not a guarantee.

Run from the repo root:
    .venv\\Scripts\\python.exe scripts\\export_finetune_data.py --names "Kiran,Girija"
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Optional

ROOT = Path(__file__).resolve().parent.parent
VOTES = ROOT / "data" / "preference_votes.jsonl"
LOG = ROOT / "data" / "training_log.jsonl"
OUT = ROOT / "data" / "finetune"

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?<![\w.])\+?\d[\d ()-]{8,}\d(?![\w.])")
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_USER_DIR = re.compile(r"(?i)([A-Z]:[\\/]+Users[\\/]+)[^\\/\s\"']+")


def _phone(m: re.Match) -> str:
    """Ten or more digits, and not a date or a version-like run: '2026-10-20' and '1.4.4' stay."""
    raw = m.group(0)
    if sum(c.isdigit() for c in raw) < 10 or _DATE.search(raw):
        return raw
    return "<phone>"


class Redactor:
    def __init__(self, names: Iterable[str] = ()):
        words = sorted({n.strip() for n in names if n and n.strip()}, key=len, reverse=True)
        self.names = re.compile(r"(?i)\b(?:" + "|".join(map(re.escape, words)) + r")\b") if words else None
        self.counts: Counter = Counter()

    def text(self, value: str) -> str:
        def sub(pattern: re.Pattern, repl, s: str, label: str) -> str:
            out, n = pattern.subn(repl, s)
            self.counts[label] += n if label != "phone" else out.count("<phone>") - s.count("<phone>")
            return out

        s = sub(_EMAIL, "<email>", value, "email")
        s = sub(_USER_DIR, r"\1<user>", s, "user_dir")
        s = sub(_PHONE, _phone, s, "phone")
        if self.names:
            s = sub(self.names, "<name>", s, "name")
        return s

    def any(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.any(v) for v in value]
        if isinstance(value, dict):
            return {k: self.any(v) for k, v in value.items()}
        return value


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            print(f"warning: {path.name} line {n} is not JSON, skipped", file=sys.stderr)
    return rows


def dpo_rows(votes: list[dict], red: Redactor, dropped: Counter) -> list[dict]:
    latest: dict[str, dict] = {}
    for v in votes:  # a later vote on the same pair replaces the earlier one
        latest[str(v.get("pair") or id(v))] = v
    out = []
    for key, v in latest.items():
        chosen, rejected, prompt = (str(v.get(k) or "").strip() for k in ("chosen", "rejected", "prompt"))
        if v.get("tie"):
            dropped["tie"] += 1
        elif not prompt or not chosen or not rejected:
            dropped["empty side or prompt"] += 1
        elif chosen == rejected:
            dropped["identical answers"] += 1
        elif v.get("earlier_turns") and not all(isinstance(t, dict) for t in v["earlier_turns"]):
            dropped["multi-turn (answers between turns not stored)"] += 1
        elif v.get("chosen_passed") is False and v.get("rejected_passed") is True:
            dropped["chosen failed the check, rejected passed"] += 1
        else:
            context = [{"role": t.get("role"), "content": red.text(str(t.get("content") or ""))}
                       for t in v.get("earlier_turns") or []]
            out.append({"id": key, "source": v.get("source") or "review-page",
                        # an in-app vote from before compare.py stored the context: the reply may lean on turns not here
                        "context_stored": bool(context) or v.get("source") != "in-app",
                        "prompt": context + [{"role": "user", "content": red.text(prompt)}],
                        "chosen": [{"role": "assistant", "content": red.text(chosen)}],
                        "rejected": [{"role": "assistant", "content": red.text(rejected)}],
                        "chosen_model": v.get("chosen_model"), "rejected_model": v.get("rejected_model")})
    return out


def review_rows(log: list[dict], red: Redactor) -> list[dict]:
    flags: dict[str, dict] = {}
    out = []
    for r in log:
        kind = r.get("kind")
        if kind == "wrong":
            flags[r.get("turn_id")] = r          # a later flag on the same turn supersedes the earlier one
        elif kind == "unflagged":
            flags.pop(r.get("turn_id"), None)
        elif kind == "net":
            out.append({"kind": "net", "turn_id": r.get("turn_id"), "nets": r.get("nets"), "model": r.get("model"),
                        "user_message": r.get("user_message"), "sent_back": r.get("sent_back"), "tools": r.get("tools"),
                        "kept_reply": r.get("reply"),
                        "use": "write the reply the model should have given first; SFT on it, or DPO against the draft"})
    for r in flags.values():
        out.append({"kind": "wrong", "turn_id": r.get("turn_id"), "model": r.get("model"), "user_message": r.get("user_message"),
                    "tools": r.get("tools"), "reply": r.get("reply"), "meant": r.get("meant") or "",
                    "use": "write the right reply (from 'meant'); DPO it against 'reply'"})
    flagged = {r["turn_id"] for r in out if r["kind"] == "wrong"}
    for r in out:  # a net turn the user also flagged: the kept reply was wrong too
        if r["kind"] == "net" and r["turn_id"] in flagged:
            r["kept_reply_flagged_wrong"] = True
    return [red.any(r) for r in out]


def export(votes_path: Path = VOTES, log_path: Path = LOG, out_dir: Path = OUT, names: Iterable[str] = ()) -> dict:
    red = Redactor(names)
    dropped: Counter = Counter()
    log = read_jsonl(log_path)
    dpo = dpo_rows(read_jsonl(votes_path), red, dropped)
    review = review_rows(log, red)
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, rows in (("dpo.jsonl", dpo), ("review.jsonl", review)):
        tmp = out_dir / (name + ".tmp")
        tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        tmp.replace(out_dir / name)
    manifest = {
        "dpo_pairs": len(dpo),
        "dpo_by_source": dict(Counter(r["source"] for r in dpo)),
        "dpo_in_app_without_context": sum(1 for r in dpo if not r["context_stored"]),
        "dropped_votes": dict(dropped),
        "review_rows": dict(Counter(r["kind"] for r in review)),
        "captures_in_log": sum(1 for r in log if r.get("kind") == "capture"),
        "redactions": dict(red.counts),
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--votes", type=Path, default=VOTES)
    ap.add_argument("--log", type=Path, default=LOG)
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--names", default="", help="comma-separated names to replace with <name>")
    args = ap.parse_args(argv)
    manifest = export(args.votes, args.log, args.out, args.names.split(","))
    print(json.dumps(manifest, indent=2))
    print(f"written to {args.out}  (read review.jsonl before anything leaves this PC)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
