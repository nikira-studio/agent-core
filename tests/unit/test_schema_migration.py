"""Unit tests for the status_changed_at migration/backfill in app.schema.

This only matters for upgrading an existing database that has retracted/
superseded rows from before the column existed (exactly the situation any
already-deployed Agent Core instance is in). A fresh test DB always gets the
column at creation time via the same migration function, so these tests
exercise it directly against a hand-built "pre-migration" table rather than
via the normal clean_db fixture (which can't represent the pre-upgrade state).
"""

import sqlite3

from app.schema import SCHEMA_SQL, _ensure_memory_metadata_columns, _ensure_user_active_column


def _bare_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    return conn


def _insert_legacy_record(conn, record_id: str, record_status: str) -> None:
    conn.execute(
        """
        INSERT INTO memory_records (id, content, memory_class, scope, created_at, record_status)
        VALUES (?, 'legacy content', 'fact', 'agent:testagent', '2020-01-01T00:00:00+00:00', ?)
        """,
        (record_id, record_status),
    )
    conn.commit()


def test_status_changed_at_column_missing_before_migration():
    conn = _bare_connection()
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(memory_records)").fetchall()}
    assert "status_changed_at" not in columns


def test_migration_adds_column_and_backfills_legacy_retracted_rows():
    conn = _bare_connection()
    _insert_legacy_record(conn, "legacy-retracted", "retracted")
    _insert_legacy_record(conn, "legacy-superseded", "superseded")
    _insert_legacy_record(conn, "legacy-active", "active")

    _ensure_memory_metadata_columns(conn)

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(memory_records)").fetchall()}
    assert "status_changed_at" in columns

    rows = {
        row["id"]: row["status_changed_at"]
        for row in conn.execute("SELECT id, status_changed_at FROM memory_records").fetchall()
    }
    # Retracted/superseded legacy rows get a fresh "now" stamp, not backdated
    # to created_at (2020) -- backdating would make them immediately
    # purge-eligible with zero grace period.
    assert rows["legacy-retracted"] is not None
    assert not rows["legacy-retracted"].startswith("2020")
    assert rows["legacy-superseded"] is not None
    assert not rows["legacy-superseded"].startswith("2020")
    # Active rows have nothing to backfill -- the purge logic only ever looks
    # at retracted/superseded rows, so this should stay NULL.
    assert rows["legacy-active"] is None


def test_migration_is_idempotent_and_does_not_reset_real_timestamps():
    """create_schema() (and thus this migration function) runs on every app
    startup, not just once. If it re-ran the backfill on every startup, it
    would stomp a real retraction timestamp recorded by retract_memory back
    to "now" on every restart."""
    conn = _bare_connection()
    _insert_legacy_record(conn, "legacy-retracted", "retracted")

    _ensure_memory_metadata_columns(conn)
    first_stamp = conn.execute(
        "SELECT status_changed_at FROM memory_records WHERE id = 'legacy-retracted'"
    ).fetchone()["status_changed_at"]

    # A real, later retraction timestamp -- must survive a second migration call.
    conn.execute(
        "UPDATE memory_records SET status_changed_at = '2099-01-01T00:00:00+00:00' WHERE id = 'legacy-retracted'"
    )
    conn.commit()

    _ensure_memory_metadata_columns(conn)

    second_stamp = conn.execute(
        "SELECT status_changed_at FROM memory_records WHERE id = 'legacy-retracted'"
    ).fetchone()["status_changed_at"]
    assert second_stamp == "2099-01-01T00:00:00+00:00"
    assert second_stamp != first_stamp


def test_user_active_migration_defaults_existing_accounts_to_active():
    conn = _bare_connection()
    conn.execute("ALTER TABLE users DROP COLUMN is_active")
    conn.execute(
        "INSERT INTO users (id, email, password_hash, display_name, role) VALUES ('legacy', 'legacy@test.local', 'hash', 'Legacy', 'user')"
    )
    conn.commit()

    _ensure_user_active_column(conn)
    row = conn.execute("SELECT is_active FROM users WHERE id = 'legacy'").fetchone()
    assert row["is_active"] == 1

    _ensure_user_active_column(conn)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    assert "is_active" in columns
