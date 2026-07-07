"""
One-off utility: locate and delete the BOB JUICE SQLite database.

The app uses bob_juice.db in the project folder (NOT database.db).

Usage:
    py -3 reset_database.py
    py -3 reset_database.py --yes          # skip confirmation
    set BOB_RESET_DB=1 && py -3 main.py    # auto-reset on next server start
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from config import DB_PATH, settings


def sqlite_sidecars(db_path: Path) -> list[Path]:
    """WAL mode creates -wal and -shm siblings next to the main file."""
    return [db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")]


def locate_database_files() -> list[Path]:
    """Return existing SQLite files for this app."""
    candidates = sqlite_sidecars(DB_PATH.resolve())
    # Also scan project root for any *.db in case DATABASE_URL was overridden
    project = Path(__file__).resolve().parent
    for p in project.glob("*.db"):
        candidates.extend(sqlite_sidecars(p.resolve()))
    seen: set[Path] = set()
    existing: list[Path] = []
    for p in candidates:
        if p in seen:
            continue
        seen.add(p)
        if p.exists():
            existing.append(p)
    return existing


def delete_database_files() -> list[Path]:
    removed: list[Path] = []
    for path in locate_database_files():
        path.unlink(missing_ok=True)
        removed.append(path)
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description="Delete BOB JUICE local SQLite database")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")
    args = parser.parse_args()

    print("BOB JUICE database reset utility")
    print(f"  Configured path : {DB_PATH.resolve()}")
    print(f"  Database URL    : {settings.database_url}")

    files = locate_database_files()
    if not files:
        print("\nNo database files found. Nothing to delete.")
        print(f"Expected location: {DB_PATH.resolve()}")
        return 0

    print("\nFiles to delete:")
    for f in files:
        print(f"  - {f} ({f.stat().st_size:,} bytes)")

    if not args.yes:
        answer = input("\nDelete these files? All local POS data will be lost. [y/N]: ").strip().lower()
        if answer not in ("y", "yes"):
            print("Cancelled.")
            return 1

    removed = delete_database_files()
    print(f"\nDeleted {len(removed)} file(s). Restart the server to recreate a fresh schema.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
