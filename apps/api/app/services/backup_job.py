"""命令行备份/恢复演练。"""

from __future__ import annotations

import argparse
from pathlib import Path

from app.config import get_settings
from app.db.session import SessionLocal
from app.services.backup import create_backup, restore_to_new_directory, verify_backup


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    backup_parser = subparsers.add_parser("create")
    backup_parser.add_argument("destination", type=Path)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("directory", type=Path)
    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("directory", type=Path)
    restore_parser.add_argument("target", type=Path)
    args = parser.parse_args()
    if args.command == "verify":
        print(verify_backup(args.directory))
        return
    if args.command == "restore":
        print(restore_to_new_directory(args.directory, args.target))
        return
    settings = get_settings()
    db = SessionLocal()
    try:
        print(
            create_backup(
                db,
                database_url=settings.database_url,
                research_data_dir=Path(settings.research_data_dir),
                destination=args.destination,
            )
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
