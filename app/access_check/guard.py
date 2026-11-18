"""Apply a schema revision that touches who can reach what, and refuse it if access changed unexpectedly.

The revision runs in one transaction on the connection that migrates. Inside it: capture access, run the revision,
capture access again, compare. Any difference the revision did not declare, or any failed check, rolls the whole
transaction back (the database is left exactly as it was, at the previous revision) and raises ``MigrationRefused``,
so the application does not start on a half-changed database. A report with ids and names only is written next to the
database for a person to read.

Where the revision cannot be atomic (nothing in the guarded revisions needs a table rebuild), the written recovery
procedure in docs/upgrading.md applies; a copy of the database taken just before is kept as
``pre-migration-<revision>-<time>.db`` beside it.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from app.access_check import compare, snapshot

logger = logging.getLogger(__name__)


class MigrationRefused(RuntimeError):
    """A guarded revision changed access in a way it did not declare. Nothing was applied."""


@dataclass
class RevisionCheck:
    """What a revision declares: the paths that may differ, and extra checks over the two snapshots."""

    allowed: tuple[str, ...] = ()
    # How "before" is computed, when the previous rule is not the current code's (for example use_rule="1.3").
    before: dict = field(default_factory=dict)
    validate: Callable[[dict, dict], list[str]] = field(default=lambda before, after: [])
    notes: Callable[[dict, dict], list[str]] = field(default=lambda before, after: [])


# Every guarded revision applied in this process, for the dry-run tool and tests to print.
SUMMARIES: list[dict] = []


def _database_file(conn) -> Path | None:
    for row in conn.execute("PRAGMA database_list").fetchall():
        if row[1] == "main" and row[2]:
            return Path(row[2])
    return None


def _new_private_file(folder: Path, stem: str, suffix: str) -> Path:
    """Create an empty file that only its owner can read, and return its path. It exists with mode 0600 before any
    content is written to it (a umask can only narrow that); a name already taken gets a counter."""
    for attempt in range(100):
        candidate = folder / (f"{stem}{suffix}" if attempt == 0 else f"{stem}-{attempt}{suffix}")
        try:
            os.close(os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            return candidate
        except FileExistsError:
            continue
    raise FileExistsError(f"no free name for {stem}{suffix}")


def _backup_before(conn, revision: int) -> Path | None:
    """Copy the database aside before a guarded revision runs, if there is anything worth keeping."""
    path = _database_file(conn)
    if path is None or not conn.execute("SELECT 1 FROM agents LIMIT 1").fetchone():
        return None
    target = _new_private_file(path.parent, f"pre-migration-{revision}-{time.strftime('%Y%m%d%H%M%S')}", ".db")
    dest = sqlite3.connect(str(target))
    try:
        conn.backup(dest)
    finally:
        dest.close()
    os.chmod(target, 0o600)
    older = sorted(path.parent.glob(f"pre-migration-{revision}-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in older[2:]:
        try:
            stale.unlink()
        except OSError:
            pass
    return target


def _write_report(conn, revision: int, name: str, problems: list[str], diffs: list[dict], before: dict | None, after: dict | None) -> Path | None:
    path = _database_file(conn)
    if path is None:  # an in-memory database has nowhere to keep a report
        return None
    folder = path.parent / "migration-reports"
    try:
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        report = _new_private_file(folder, f"revision-{revision}-{time.strftime('%Y%m%d%H%M%S')}", ".json")
        report.write_text(json.dumps({
            "revision": revision,
            "name": name,
            "problems": problems,
            "differences": diffs,
            "counts_before": snapshot.counts(before) if before else None,
            "counts_after": snapshot.counts(after) if after else None,
        }, indent=2, default=str))
        os.chmod(report, 0o600)
        return report
    except OSError:
        logger.exception("could not write the migration report")
        return None


def run_guarded(conn, revision: int, name: str, migrate: Callable, check: RevisionCheck | None = None) -> dict:
    """Apply one revision under the access check. Returns the report summary (counts, notes) on success."""
    check = check or RevisionCheck()
    conn.commit()
    backup = _backup_before(conn, revision)
    conn.execute("BEGIN IMMEDIATE")
    before = after = None
    try:
        before = snapshot.capture(conn, **check.before)
        migrate(conn)
        after = snapshot.capture(conn)
        diffs = compare.differences(before, after, check.allowed)
        problems = check.validate(before, after)
        if diffs or problems:
            report = _write_report(conn, revision, name, problems, diffs, before, after)
            detail = "; ".join(problems) or compare.summarise(diffs, 5)
            raise MigrationRefused(
                f"Upgrade step {revision} ({name}) was refused and not applied: access would have changed "
                f"({len(diffs)} difference(s), {len(problems)} failed check(s)). {detail} "
                f"Nothing was changed; the database is at the previous version. "
                f"Report: {report or 'not written'}. Copy before the step: {backup or 'none'}."
            )
        conn.execute("INSERT INTO schema_migrations (revision, name) VALUES (?, ?)", (revision, name))
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    notes = check.notes(before, after)
    summary = {
        "revision": revision,
        "name": name,
        "counts_before": snapshot.counts(before),
        "counts": snapshot.counts(after),
        "notes": notes,
        "backup": str(backup) if backup else None,
    }
    SUMMARIES.append(summary)
    logger.info("schema revision %s (%s) applied under the access check: %s", revision, name, summary)
    return summary
