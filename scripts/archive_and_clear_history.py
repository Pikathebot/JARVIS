"""Archive every stored conversation, then clear them (PLAN 4.18 phase 5, decision 6).

Everything chatted before the long thread is testing, project chats included: it is archived to a
dated folder under the repo's data/ (a full copy of the database plus a readable JSON dump of the
conversations), then sessions, messages, thread episodes, compaction events and attachment rows
are deleted. Projects (folders, files, instructions), memories and artifacts stay; memories and
artifacts only lose the link to the chat they came from.

    python scripts/archive_and_clear_history.py          # dry run: counts, writes nothing
    python scripts/archive_and_clear_history.py --yes    # archive, then clear

Close Jarvis first: the script refuses while the backend is listening on :8000.
Standard library only -- it never imports the app (no accounts, no models touched).
"""
from __future__ import annotations

import argparse
import json
import socket
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "backend" / "data" / "jarvis_memory.db"
DEFAULT_OUT = ROOT / "data" / "history-archive"

# Conversation tables, archived and emptied (children before parents).
CLEARED = ["messages", "episodes", "compaction_events", "attachments", "tool_calls", "agent_runs", "sessions"]
# Kept, but their link to a deleted chat is dropped.
UNLINKED = {"memories": "source_session_id", "artifacts": "session_id", "file_versions": "session_id"}


def backend_running(port: int = 8000) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--yes", action="store_true", help="archive and clear (without it: dry run)")
    args = ap.parse_args()

    if not args.db.exists():
        print(f"No database at {args.db}")
        return 1
    if args.yes and args.db.resolve() == DEFAULT_DB.resolve() and backend_running():
        print("Jarvis's backend is running (port 8000). Close Jarvis, then run this again.")
        return 1

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    present = tables(conn)
    print(f"Database: {args.db}")
    for t in CLEARED:
        if t in present:
            print(f"  {t:18} {conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]:6} rows -> deleted")
    for t, column in UNLINKED.items():
        if t in present:
            n = conn.execute(f"SELECT COUNT(*) FROM {t} WHERE {column} IS NOT NULL").fetchone()[0]
            print(f"  {t:18} {n:6} rows keep, lose their {column}")
    kept = [t for t in ("projects", "memories", "artifacts", "artifact_versions", "documents") if t in present]
    print("  kept as they are: " + ", ".join(kept))
    if not args.yes:
        print("\nDry run: nothing written. Run with --yes to archive and clear.")
        return 0

    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    folder = args.out / stamp
    folder.mkdir(parents=True, exist_ok=False)
    copy = sqlite3.connect(folder / "jarvis_memory.db")
    conn.backup(copy)  # the whole database, exactly as it was
    copy.close()
    dump = {t: [dict(r) for r in conn.execute(f"SELECT * FROM {t}")]
            for t in CLEARED if t in present}
    for row in dump.get("episodes", []):
        row.pop("embedding", None)  # vectors are no use to a reader; the .db copy keeps them
    (folder / "conversations.json").write_text(json.dumps(dump, ensure_ascii=False, indent=1, default=str),
                                              encoding="utf-8")
    print(f"\nArchived to {folder}")

    with conn:
        for t, column in UNLINKED.items():
            if t in present:
                conn.execute(f"UPDATE {t} SET {column} = NULL WHERE {column} IS NOT NULL")
        for t in CLEARED:
            if t in present:
                conn.execute(f"DELETE FROM {t}")  # the messages FTS index follows by trigger
    if "messages_fts" in present:
        with conn:
            conn.execute("INSERT INTO messages_fts(messages_fts) VALUES ('rebuild')")
    conn.execute("VACUUM")
    left = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in CLEARED if t in present}
    conn.close()
    print("Cleared: " + ", ".join(f"{t} {n}" for t, n in left.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
