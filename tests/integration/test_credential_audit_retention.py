"""Retention and idempotency tests for credential-resolution audit rows."""

from datetime import datetime, timedelta, timezone

from app.database import get_db
from app.services import audit_service
from app.services.credential_audit_retention_service import apply_retention
from app.services import backup_service, system_settings_service


def _stamp_event(*, action, actor_type="agent", actor_id="consumer-a", resource_id="entry-a", result="success", age_days=0):
    event_id = audit_service.write_event(
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        resource_type="credential",
        resource_id=resource_id,
        result=result,
    )
    timestamp = (datetime.now(timezone.utc) - timedelta(days=age_days)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    with get_db() as conn:
        conn.execute("UPDATE audit_log SET timestamp=? WHERE id=?", (timestamp, event_id))
    return event_id


def test_aged_successes_roll_into_daily_counts_and_recent_rows_stay_raw(clean_db):
    old_a = _stamp_event(action="credential_consumer_resolved", age_days=31)
    old_b = _stamp_event(action="credential_consumer_resolved", age_days=31)
    broker = _stamp_event(
        action="broker_resolve",
        actor_type="broker",
        actor_id="broker-a",
        resource_id="entry-b",
        age_days=31,
    )
    recent = _stamp_event(action="credential_consumer_resolved", age_days=29)
    denied = _stamp_event(
        action="credential_consumer_denied", result="blocked", age_days=31
    )

    result = apply_retention()

    assert result["resolution_rows_compacted"] == 3
    assert result["credential_audit_events_pruned"] == 0
    with get_db() as conn:
        raw_ids = {row[0] for row in conn.execute("SELECT id FROM audit_log")}
        counts = {
            (row["action"], row["actor_id"], row["credential_entry_id"]): row[
                "success_count"
            ]
            for row in conn.execute("SELECT * FROM credential_resolution_daily")
        }
        receipt = conn.execute(
            "SELECT details_json FROM audit_log WHERE action='credential_resolution_retention_applied'"
        ).fetchone()

    assert old_a not in raw_ids and old_b not in raw_ids and broker not in raw_ids
    assert recent in raw_ids and denied in raw_ids
    assert counts[("credential_consumer_resolved", "consumer-a", "entry-a")] == 2
    assert counts[("broker_resolve", "broker-a", "entry-b")] == 1
    assert receipt is not None


def test_compaction_retries_do_not_double_count_and_batches_are_bounded(clean_db):
    ids = [
        _stamp_event(action="credential_consumer_resolved", age_days=45)
        for _ in range(5)
    ]

    first = apply_retention(batch_size=2)
    assert first["resolution_rows_compacted"] == 2
    assert first["resolution_compaction_backlog"] is True
    second = apply_retention(batch_size=2)
    assert second["resolution_rows_compacted"] == 2
    third = apply_retention(batch_size=2)
    assert third["resolution_rows_compacted"] == 1
    fourth = apply_retention(batch_size=2)
    assert fourth["resolution_rows_compacted"] == 0

    with get_db() as conn:
        assert conn.execute(
            "SELECT SUM(success_count) FROM credential_resolution_daily"
        ).fetchone()[0] == 5
        remaining = {
            row[0] for row in conn.execute("SELECT id FROM audit_log")
        }
    assert not set(ids) & remaining


def test_denials_grants_credential_changes_and_key_events_stay_individual_13_months(clean_db):
    retained = [
        _stamp_event(action="credential_consumer_denied", result="blocked", age_days=390),
        _stamp_event(action="credential_consumer_grant_created", age_days=390),
        _stamp_event(action="credential_consumer_grant_revoked", age_days=390),
        _stamp_event(action="credential_entry_updated", age_days=390),
        _stamp_event(action="credential_key_rotated", age_days=390),
        _stamp_event(action="agent_key_rotated", age_days=390),
        _stamp_event(action="inbound_key_rotated", age_days=390),
        _stamp_event(action="scope_denied", age_days=390),
        _stamp_event(action="memory_write", result="failure", age_days=390),
    ]
    expired = [
        _stamp_event(action="credential_consumer_denied", result="blocked", age_days=430),
        _stamp_event(action="credential_entry_deleted", age_days=430),
        _stamp_event(action="credential_key_restored", age_days=430),
    ]
    unrelated = _stamp_event(action="memory_write", age_days=430)

    result = apply_retention()

    assert result["credential_audit_events_pruned"] == len(expired)
    with get_db() as conn:
        remaining = {row[0] for row in conn.execute("SELECT id FROM audit_log")}
    assert set(retained) <= remaining
    assert not set(expired) & remaining
    assert unrelated in remaining


def test_rollups_expire_after_13_months(clean_db):
    with get_db() as conn:
        conn.execute(
            """INSERT INTO credential_resolution_daily
               (resolution_date, action, actor_type, actor_id, credential_entry_id, success_count)
               VALUES ('2024-01-01', 'credential_consumer_resolved', 'agent', 'old-agent', 'old-entry', 9)"""
        )
        conn.execute(
            """INSERT INTO credential_resolution_daily
               (resolution_date, action, actor_type, actor_id, credential_entry_id, success_count)
               VALUES ('2026-09-01', 'credential_consumer_resolved', 'agent', 'new-agent', 'new-entry', 3)"""
        )

    result = apply_retention()
    assert result["resolution_aggregate_rows_pruned"] == 1
    with get_db() as conn:
        rows = conn.execute(
            "SELECT actor_id, success_count FROM credential_resolution_daily"
        ).fetchall()
    assert [(row["actor_id"], row["success_count"]) for row in rows] == [
        ("new-agent", 3)
    ]


def test_scheduled_maintenance_reports_retention_run(clean_db):
    system_settings_service.write_raw(
        {"verification_pass_enabled": "0", "consolidation_scan_enabled": "0"}
    )

    result = backup_service.run_scheduled_maintenance(triggered_by="test")

    assert result["credential_audit_retention"]["ok"] is True
    assert result["credential_audit_retention"]["resolution_rows_compacted"] == 0
    assert result["credential_audit_retention"]["resolution_compaction_backlog"] is False
