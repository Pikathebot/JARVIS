"""Back up or restore Jarvis's state (PLAN 4.29 B6).

    .venv\\Scripts\\python.exe scripts\\backup.py backup [dest.zip] [--include-secrets]
    .venv\\Scripts\\python.exe scripts\\backup.py restore src.zip [--overwrite]

Restore with Jarvis closed. RAG indexes are not in the backup; reopen each project to rebuild them.
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.backup import BackupError, create_backup, default_locations, restore_backup  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    make = sub.add_parser("backup")
    make.add_argument("dest", nargs="?", type=Path)
    make.add_argument("--include-secrets", action="store_true")
    back = sub.add_parser("restore")
    back.add_argument("source", type=Path)
    back.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    try:
        if args.cmd == "backup":
            dest = args.dest or ROOT / "backups" / f"jarvis-backup-{datetime.now():%Y%m%d-%H%M%S}.zip"
            report = create_backup(default_locations(), dest, include_secrets=args.include_secrets)
            print(f"{report.files} files, {report.bytes_written / 1e6:.1f} MB -> {report.path}")
        else:
            count = restore_backup(args.source, default_locations(), overwrite=args.overwrite)
            print(f"restored {count} files")
    except BackupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
