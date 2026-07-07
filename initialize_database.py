"""
Delete bob_juice.db (and WAL sidecars) then recreate schema + seed data.

Usage:
    python initialize_database.py
"""

from __future__ import annotations

import asyncio
import sys

from database import init_db, reset_local_database_files


async def main() -> int:
    removed = reset_local_database_files()
    if removed:
        print(f"Removed {len(removed)} database file(s):")
        for path in removed:
            print(f"  - {path}")
    else:
        print("No existing database files — creating fresh schema.")

    await init_db()
    print("Fresh bob_juice.db initialized with core models and seed data.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
