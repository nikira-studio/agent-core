"""Dry run: apply the pending guarded upgrade steps to a COPY of a database and print the access comparison.

    python -m app.access_check path/to/agent-core.db

The file is opened read-only and copied to a temporary folder; the copy is what gets migrated, and it is deleted
afterwards (``--keep`` keeps it). The original is never written. Output holds ids, names and counts only.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

from app.access_check import guard, snapshot
from app.access_check.guard import MigrationRefused


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.access_check", description=__doc__)
    parser.add_argument("database", help="the database file to check (read-only; a copy is migrated)")
    parser.add_argument("--keep", action="store_true", help="keep the migrated copy and print its path")
    args = parser.parse_args(argv)

    source = Path(args.database).resolve()
    if not source.is_file():
        print(f"no such file: {source}", file=sys.stderr)
        return 2
    folder = Path(tempfile.mkdtemp(prefix="access-check-"))
    copy = folder / "copy.db"
    try:
        origin = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        target = sqlite3.connect(str(copy))
        try:
            origin.backup(target)
        finally:
            target.close()
            origin.close()
        conn = sqlite3.connect(str(copy))
        conn.row_factory = sqlite3.Row
        try:
            from app.schema import create_schema

            create_schema(conn)
        except MigrationRefused as refused:
            print("REFUSED")
            print(refused)
            return 1
        finally:
            conn.close()
        if not guard.SUMMARIES:
            print("No guarded upgrade step was pending: nothing to compare.")
            return 0
        for item in guard.SUMMARIES:
            print(f"Step {item['revision']} ({item['name']}): applied, access identical before and after.")
            print(f"  before: {item['counts_before']}")
            print(f"  after:  {item['counts']}")
            for note in item["notes"]:
                print(f"  note: {note}")
        conn = sqlite3.connect(str(copy))
        conn.row_factory = sqlite3.Row
        try:
            for row in conn.execute("SELECT kind, id FROM agents ORDER BY kind, id"):
                print(f"  {row['kind']:8} {row['id']}")
        finally:
            conn.close()
        return 0
    finally:
        if args.keep:
            print(f"copy kept at {copy}")
        else:
            shutil.rmtree(folder, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
