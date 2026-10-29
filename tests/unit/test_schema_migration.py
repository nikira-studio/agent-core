"""Unit tests for the status_changed_at migration/backfill in app.schema.

This only matters for upgrading an existing database that has retracted/
superseded rows from before the column existed (exactly the situation any
already-deployed Agent Core instance is in). A fresh test DB always gets the
column at creation time via the same migration function, so these tests
exercise it directly against a hand-built "pre-migration" table rather than
via the normal clean_db fixture (which can't represent the pre-upgrade state).
"""

import sqlite3

import app.schema as schema_module

from app.schema import (
    SCHEMA_SQL,
    _ensure_memory_metadata_columns,
    _ensure_user_active_column,
    create_schema,
)
from app.services import system_settings_service


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
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(memory_records)").fetchall()
    }
    assert "status_changed_at" not in columns


def test_migration_adds_column_and_backfills_legacy_retracted_rows():
    conn = _bare_connection()
    _insert_legacy_record(conn, "legacy-retracted", "retracted")
    _insert_legacy_record(conn, "legacy-superseded", "superseded")
    _insert_legacy_record(conn, "legacy-active", "active")

    _ensure_memory_metadata_columns(conn)

    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(memory_records)").fetchall()
    }
    assert "status_changed_at" in columns

    rows = {
        row["id"]: row["status_changed_at"]
        for row in conn.execute(
            "SELECT id, status_changed_at FROM memory_records"
        ).fetchall()
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
    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()
    }
    assert "is_active" in columns


def test_existing_binding_table_adds_resolution_columns_before_indexes():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE connector_bindings (
            id TEXT PRIMARY KEY, connector_type_id TEXT NOT NULL, name TEXT NOT NULL,
            scope TEXT NOT NULL, credential_id TEXT, config_json TEXT,
            rate_limit_config_json TEXT, enabled INTEGER NOT NULL DEFAULT 1,
            last_tested_at TEXT, last_error TEXT, created_by TEXT,
            created_at TEXT, updated_at TEXT
        )"""
    )
    conn.commit()

    create_schema(conn)

    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(connector_bindings)")
    }
    indexes = {
        row["name"] for row in conn.execute("PRAGMA index_list(connector_bindings)")
    }
    assert "logical_alias" in columns
    assert "is_preferred" in columns
    assert "idx_bindings_alias_unique" in indexes
    assert "idx_bindings_preferred_unique" in indexes


def test_existing_webhook_delivery_table_is_migrated_before_current_indexes():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE webhook_delivery_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            webhook_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('success', 'failure')),
            http_status INTEGER,
            error_message TEXT,
            delivered_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX idx_webhook_delivery_webhook
            ON webhook_delivery_log(webhook_id, delivered_at DESC);
        INSERT INTO webhook_delivery_log
            (webhook_id, event_type, payload_json, status, http_status, error_message,
             delivered_at)
        VALUES
            ('webhook-1', 'activity_updated', '{}', 'failure', 503, 'unavailable',
             '2026-09-05T00:00:00+00:00');
        """
    )

    create_schema(conn)

    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(webhook_delivery_log)")
    }
    indexes = {
        row["name"] for row in conn.execute("PRAGMA index_list(webhook_delivery_log)")
    }
    delivery = conn.execute(
        "SELECT event_id, status, attempt_count, created_at, delivered_at "
        "FROM webhook_delivery_log WHERE webhook_id = 'webhook-1'"
    ).fetchone()

    assert {"event_id", "next_attempt_at", "lease_expires_at", "created_at"} <= columns
    assert {"idx_webhook_delivery_webhook", "idx_webhook_delivery_due"} <= indexes
    assert delivery["event_id"]
    assert delivery["status"] == "dead"
    assert delivery["attempt_count"] == 1
    assert delivery["created_at"] == delivery["delivered_at"]


def test_create_schema_records_and_skips_applied_revision(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    create_schema(conn)

    revisions = [
        dict(row)
        for row in conn.execute(
            "SELECT revision, name FROM schema_migrations ORDER BY revision"
        )
    ]
    assert revisions == [
        {"revision": 1, "name": "normalize-current-schema"},
        {"revision": 2, "name": "canonical-system-settings"},
        {"revision": 3, "name": "agent-capabilities-and-webhook-queue"},
        {"revision": 4, "name": "system-settings-default-rows"},
        {"revision": 5, "name": "unconfirmed-inference-importance-gate"},
        {"revision": 6, "name": "credential-consumer-explicit-write-grants"},
        {"revision": 7, "name": "credential-resolution-audit-retention"},
        {"revision": 8, "name": "quiet-unattended-confirm-changes"},
        {"revision": 9, "name": "execution-log-without-response-bodies"},
        {"revision": 10, "name": "anchor-checks-are-not-confirmations"},
        {"revision": 11, "name": "drop-delegation-tables"},
        {"revision": 12, "name": "structured-activity-handoffs"},
        {"revision": 13, "name": "remove-handoff-briefings"},
        {"revision": 14, "name": "drop-removed-feature-settings"},
        {"revision": 15, "name": "single-use-otp-codes"},
    ]
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='credential_resolution_daily'"
    ).fetchone()

    def unexpected_rerun(_conn):
        raise AssertionError("an applied migration ran again")

    monkeypatch.setattr(schema_module, "_migrate_001_current_schema", unexpected_rerun)
    create_schema(conn)


def test_migration_005_bumps_only_the_unmodified_default(monkeypatch):
    """An install that already migrated to revision 4 has an explicit
    unconfirmed_inference_days='7' row. Migration 5 must bump that to the
    new 90-day default, but must never touch a value an operator already
    changed away from '7'."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    create_schema(conn)

    def read(key):
        row = conn.execute(
            "SELECT value FROM system_settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    # A fresh install already lands on 90 via migration 4's current defaults.
    assert read("unconfirmed_inference_days") == "90"
    assert read("unconfirmed_inference_min_importance") == "0.7"

    # Simulate a pre-revision-5 install that still had the old seeded "7",
    # then re-run just the targeted migration.
    conn.execute(
        "UPDATE system_settings SET value = '7' WHERE key = 'unconfirmed_inference_days'"
    )
    schema_module._migrate_005_unconfirmed_inference_importance_gate(conn)
    assert read("unconfirmed_inference_days") == "90"

    # An operator's deliberate override must survive the same migration.
    conn.execute(
        "UPDATE system_settings SET value = '30' WHERE key = 'unconfirmed_inference_days'"
    )
    schema_module._migrate_005_unconfirmed_inference_importance_gate(conn)
    assert read("unconfirmed_inference_days") == "30"


def test_legacy_id_keyed_system_settings_are_rebuilt_for_current_writes(
    monkeypatch,
):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE system_settings (
            id TEXT PRIMARY KEY,
            key TEXT NOT NULL,
            value TEXT NOT NULL DEFAULT '',
            value_encrypted TEXT,
            updated_at TEXT
        )"""
    )
    conn.execute(
        "INSERT INTO system_settings (id, key, value, value_encrypted) "
        "VALUES ('vector', 'vector_api_key', '', 'encrypted-legacy')"
    )
    conn.commit()

    create_schema(conn)

    columns = {
        row["name"]: row for row in conn.execute("PRAGMA table_info(system_settings)")
    }
    assert columns["key"]["pk"] == 1
    assert "id" not in columns
    assert (
        conn.execute(
            "SELECT value FROM system_settings WHERE key = 'vector_api_key'"
        ).fetchone()["value"]
        == "encrypted-legacy"
    )

    monkeypatch.setattr(system_settings_service, "get_db", lambda: conn)
    system_settings_service.write_raw({"vector_model": "test-model"})
    assert (
        conn.execute(
            "SELECT value FROM system_settings WHERE key = 'vector_model'"
        ).fetchone()["value"]
        == "test-model"
    )


def test_migration_10_moves_verifier_stamps_to_anchor_checked_at():
    import sqlite3

    from app.schema import _migrate_010_anchor_checked_at

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE memory_records (id TEXT, last_confirmed_at TEXT, provenance_json TEXT);
        CREATE TABLE audit_log (action TEXT, resource_id TEXT, timestamp TEXT);
        INSERT INTO memory_records VALUES
          ('auto', '2026-10-20', '{"verified":{"by":"verification-pass"}}'),
          ('auto-after-human', '2026-10-21', '{"verified":{"by":"verification-pass"}}'),
          ('human', '2026-10-22', '{"verified":{"by":"codex"}}');
        INSERT INTO audit_log VALUES ('memory_confirmed', 'auto-after-human', '2026-09-01 10:00:00');
        """
    )
    _migrate_010_anchor_checked_at(conn)
    rows = {
        r["id"]: (r["last_confirmed_at"], r["anchor_checked_at"])
        for r in conn.execute("SELECT * FROM memory_records")
    }
    assert rows["auto"] == (None, "2026-10-20")
    assert rows["auto-after-human"] == ("2026-09-01 10:00:00", "2026-10-21")
    assert rows["human"] == ("2026-10-22", None)


DELEGATION_TABLES = (
    "delegated_grants",
    "delegated_grant_scope_permissions",
    "delegated_grant_resource_permissions",
    "delegated_grant_actions",
    "delegation_requests",
    "delegation_request_scope_permissions",
    "delegation_request_resource_permissions",
    "delegation_request_actions",
)


def _table_names(conn) -> set[str]:
    return {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def test_a_fresh_schema_has_no_delegation_tables():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    create_schema(conn)
    assert not _table_names(conn) & set(DELEGATION_TABLES)


def test_migration_11_drops_delegation_tables_from_an_existing_install():
    from app.schema import _migrate_011_drop_delegation_tables

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE delegated_grants (id TEXT PRIMARY KEY);
        CREATE TABLE delegated_grant_scope_permissions (
            grant_id TEXT REFERENCES delegated_grants(id) ON DELETE CASCADE);
        CREATE TABLE delegated_grant_resource_permissions (
            grant_id TEXT REFERENCES delegated_grants(id) ON DELETE CASCADE);
        CREATE TABLE delegated_grant_actions (
            grant_id TEXT REFERENCES delegated_grants(id) ON DELETE CASCADE);
        CREATE TABLE delegation_requests (
            id TEXT PRIMARY KEY,
            grant_id TEXT REFERENCES delegated_grants(id) ON DELETE SET NULL);
        CREATE TABLE delegation_request_scope_permissions (
            request_id TEXT REFERENCES delegation_requests(id) ON DELETE CASCADE);
        CREATE TABLE delegation_request_resource_permissions (
            request_id TEXT REFERENCES delegation_requests(id) ON DELETE CASCADE);
        CREATE TABLE delegation_request_actions (
            request_id TEXT REFERENCES delegation_requests(id) ON DELETE CASCADE);
        INSERT INTO delegated_grants VALUES ('g');
        INSERT INTO delegated_grant_actions VALUES ('g');
        INSERT INTO delegation_requests VALUES ('r', 'g');
        INSERT INTO delegation_request_actions VALUES ('r');
        """
    )
    _migrate_011_drop_delegation_tables(conn)
    assert not _table_names(conn) & set(DELEGATION_TABLES)
    # Safe to run on a database that never had them.
    _migrate_011_drop_delegation_tables(conn)


def test_migration_13_removes_briefing_rows_and_their_changes():
    from app.schema import _migrate_013_remove_handoff_briefings

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    create_schema(conn)
    rows = (
        ("real", None),
        ("keyed", '{"briefing": {"id": "keyed"}}'),
        ("typed", '{"type": "handoff_briefing"}'),
        ("other-type", '{"type": "something_else"}'),
    )
    for activity_id, metadata in rows:
        conn.execute(
            "INSERT INTO agent_activity (id, agent_id, user_id, task_description, "
            "memory_scope, started_at, metadata_json) "
            "VALUES (?, 'a1', 'u1', 'task', 'workspace:w', '2026-01-01', ?)",
            (activity_id, metadata),
        )
    conn.execute(
        "INSERT INTO workspace_changes (id, memory_scope, sequence, change_type, "
        "resource_type, resource_id, summary_json) "
        "VALUES ('legacy', 'workspace:w', 100, 'briefing_created', 'briefing', 'keyed', '{}')"
    )

    _migrate_013_remove_handoff_briefings(conn)

    remaining = {row["id"] for row in conn.execute("SELECT id FROM agent_activity")}
    assert remaining == {"real", "other-type"}
    assert not conn.execute(
        "SELECT 1 FROM workspace_changes WHERE resource_type = 'briefing'"
    ).fetchone()
    # The recreated trigger stamps every activity as an activity.
    conn.execute(
        "INSERT INTO agent_activity (id, agent_id, user_id, task_description, "
        "memory_scope, started_at, metadata_json) "
        "VALUES ('later', 'a1', 'u1', 'task', 'workspace:w', '2026-01-02', "
        "'{\"type\": \"handoff_briefing\"}')"
    )
    change = conn.execute(
        "SELECT change_type, resource_type FROM workspace_changes WHERE resource_id = 'later'"
    ).fetchone()
    assert dict(change) == {"change_type": "activity_created", "resource_type": "activity"}


def test_migration_14_drops_only_removed_feature_settings():
    from app.schema import _migrate_014_drop_removed_feature_settings

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    create_schema(conn)
    conn.execute("INSERT OR REPLACE INTO system_settings (key, value) VALUES ('review_model_url', 'http://x')")
    conn.execute("INSERT OR REPLACE INTO system_settings (key, value) VALUES ('usefulness_review_enabled', 'true')")
    conn.execute("INSERT OR REPLACE INTO system_settings (key, value) VALUES ('vector_url', 'http://v')")

    _migrate_014_drop_removed_feature_settings(conn)

    keys = {row["key"] for row in conn.execute("SELECT key FROM system_settings")}
    assert "vector_url" in keys
    assert not keys & {"review_model_url", "usefulness_review_enabled"}
