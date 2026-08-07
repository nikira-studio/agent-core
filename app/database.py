import os
import sqlite3
import threading
from pathlib import Path
from contextlib import contextmanager
from collections.abc import Generator

from app.branding import ENV_PREFIX
from app.config import settings

DB_PATH_OVERRIDE: str | None = os.environ.get(f"{ENV_PREFIX}TEST_DB")


def get_db_path() -> Path:
    if DB_PATH_OVERRIDE:
        return Path(DB_PATH_OVERRIDE)
    return settings.db_path


_sqlite_vec_available: bool | None = None


def _load_sqlite_vec(conn: sqlite3.Connection) -> None:
    global _sqlite_vec_available
    if _sqlite_vec_available is False:
        return
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        _sqlite_vec_available = True
    except Exception:
        _sqlite_vec_available = False


# ── Restore gate ─────────────────────────────────────────────────────────────
# Replacing the database file underneath a live process is not a file
# operation, it is a coordination problem. A connection opened before the swap
# keeps its handle on the old, now-unlinked inode: it accepts writes, commits
# them successfully, and nothing ever reads them again. The commit reports
# success, so nothing surfaces the loss.
#
# So a restore does not just swap files. It closes the gate, waits for work
# already in flight to finish, swaps, and reopens. Callers that arrive while it
# is closed wait; a restore that cannot drain in time refuses rather than
# proceeding over live work.
_gate = threading.Condition()
_active_units = 0
_gate_closed = False
# Held for the whole of an exclusive section. The closed-gate flag alone does
# not keep two restores apart: both would see it closed, both would proceed,
# and the first to finish would reopen the gate while the second is still
# swapping files — admitting ordinary work into a database being replaced.
_exclusive_lock = threading.Lock()


class DatabaseUnavailable(RuntimeError):
    """Raised when the database is briefly unavailable during a restore."""


def _enter_unit() -> None:
    """Take a slot, or refuse — but never wait.

    Routes reach the database from the event loop, so blocking here blocks
    every other request in the process, which is the thing the offloading work
    was for. Since the answer to an arrival during a restore is a 503 either
    way, waiting first buys nothing and costs the whole loop: it turns a
    restore into a stall for unrelated traffic. The refusal is immediate and
    carries a Retry-After.
    """
    global _active_units
    with _gate:
        if _gate_closed:
            raise DatabaseUnavailable(
                "The database is being restored; try again in a moment."
            )
        _active_units += 1


def _leave_unit() -> None:
    global _active_units
    with _gate:
        _active_units -= 1
        if _active_units == 0:
            _gate.notify_all()


@contextmanager
def exclusive_access(drain_timeout: float = 30.0) -> Generator[None, None, None]:
    """Hold the database still: no new work starts, in-flight work finishes.

    Used by restore, which replaces the file every other connection is reading.
    """
    global _gate_closed
    # One exclusive holder at a time, for the whole section — not just for the
    # drain. Two overlapping restores would otherwise take turns reopening the
    # gate underneath each other.
    if not _exclusive_lock.acquire(timeout=drain_timeout):
        raise DatabaseUnavailable(
            "Another exclusive database operation is in progress; "
            "nothing was replaced."
        )
    try:
        with _gate:
            _gate_closed = True
            drained = _gate.wait_for(lambda: _active_units == 0, drain_timeout)
            if not drained:
                _gate_closed = False
                _gate.notify_all()
                raise DatabaseUnavailable(
                    f"Database work did not finish within {drain_timeout:g}s; "
                    "nothing was replaced."
                )
        try:
            yield
        finally:
            with _gate:
                _gate_closed = False
                _gate.notify_all()
    finally:
        _exclusive_lock.release()


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(get_db_path()), timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    _load_sqlite_vec(conn)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def get_db() -> Generator[sqlite3.Connection, None, None]:
    """One unit of database work, and the thing a restore waits for.

    The gate is entered before the connection is opened and left after it is
    closed, so a restore can never begin while a connection is alive.
    """
    _enter_unit()
    try:
        conn = get_connection()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    finally:
        _leave_unit()


def init_db() -> None:
    from app.schema import create_schema

    with get_db() as conn:
        create_schema(conn)
