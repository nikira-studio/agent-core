from app.services.memory_service import write_memory, search_memory
from app.services import system_settings_service
from app.services.backup_service import (
    run_scheduled_maintenance,
    get_maintenance_status,
    try_acquire_maintenance_lock,
)
from app.database import get_db
from app.time_utils import utc_now


def _future_iso():
    from datetime import datetime, timezone, timedelta

    return (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()


def _past_iso():
    from datetime import datetime, timezone, timedelta

    return (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()


def test_expired_record_excluded_from_search(clean_db):
    write_memory(
        content="this record has expired and should not appear",
        memory_class="fact",
        scope="agent:testagent",
        expires_at=_past_iso(),
    )
    results, _ = search_memory(
        "expired should not appear", authorized_scopes=["agent:testagent"]
    )
    assert not any("expired" in r["content"] for r in results)


def test_non_expired_record_appears_in_search(clean_db):
    write_memory(
        content="this record has a future expiry and should appear",
        memory_class="fact",
        scope="agent:testagent",
        expires_at=_future_iso(),
    )
    results, _ = search_memory(
        "future expiry should appear", authorized_scopes=["agent:testagent"]
    )
    assert any("future expiry" in r["content"] for r in results)


def test_record_without_expires_at_appears_normally(clean_db):
    write_memory(
        content="no expiry set on this record",
        memory_class="fact",
        scope="agent:testagent",
    )
    results, _ = search_memory("no expiry set", authorized_scopes=["agent:testagent"])
    assert any("no expiry set" in r["content"] for r in results)


def test_an_expired_record_is_retracted_not_deleted_and_can_be_restored(clean_db):
    from app.services import audit_service, memory_service

    record, _ = write_memory(content="sweep target decision", memory_class="decision", scope="workspace:ttl", topic="Expiring decision", expires_at=_past_iso())
    run_scheduled_maintenance()
    kept = memory_service.get_memory_record(record["id"])
    assert kept is not None and kept["record_status"] == "retracted", "an expiry retracts (restorable), it does not delete"
    trace = memory_service.retraction_traces([record["id"]])[record["id"]]
    assert trace["by"] == "maintenance" and "expiry" in trace["by_label"]
    # Restoring it clears the passed expiry, so the next run does not take it back.
    assert memory_service.restore_memory(record["id"], restored_by="owner")
    restored = memory_service.get_memory_record(record["id"])
    assert restored["record_status"] == "active" and not restored["expires_at"]
    run_scheduled_maintenance()
    assert memory_service.get_memory_record(record["id"])["record_status"] == "active"


def test_ttl_sweep_does_not_delete_non_expired_records(clean_db):
    record, _ = write_memory(
        content="not yet expired record",
        memory_class="fact",
        scope="agent:testagent",
        expires_at=_future_iso(),
    )
    record_id = record["id"]

    run_scheduled_maintenance()

    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM memory_records WHERE id = ?", (record_id,)
        ).fetchone()
    assert row is not None, "Non-expired record should survive sweep"


def test_expires_at_returned_in_write_response(clean_db):
    future = _future_iso()
    record, _ = write_memory(
        content="check expires_at in response",
        memory_class="fact",
        scope="agent:testagent",
        expires_at=future,
    )
    assert record.get("expires_at") is not None


def test_maintenance_status_before_any_run(clean_db):
    status = get_maintenance_status()
    assert status["last_run_at"] is None
    assert status["last_run_by"] is None
    assert status["last_run_summary"] is None


def test_maintenance_records_last_run_status(clean_db):
    result = run_scheduled_maintenance(triggered_by="scheduled")

    status = get_maintenance_status()
    assert status["last_run_at"] is not None
    assert status["last_run_by"] == "scheduled"
    assert status["last_run_summary"] == result


def test_maintenance_default_triggered_by_is_manual(clean_db):
    run_scheduled_maintenance()

    status = get_maintenance_status()
    assert status["last_run_by"] == "manual"


def test_maintenance_status_reflects_interval_setting(clean_db, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "MAINTENANCE_INTERVAL_MINUTES", 0)
    assert get_maintenance_status()["scheduler_enabled"] is False

    monkeypatch.setattr(settings, "MAINTENANCE_INTERVAL_MINUTES", 30)
    status = get_maintenance_status()
    assert status["scheduler_enabled"] is True
    assert status["interval_minutes"] == 30


def test_scratchpad_prune_deletes_embedding_row_first(clean_db):
    """Regression test: memory_embeddings.record_id has a FK to
    memory_records(id) with foreign_keys=ON. The scratchpad-prune DELETE used
    to hit the memory_records row directly without clearing memory_embeddings
    first, raising sqlite3.IntegrityError for any embedded (vector-search-
    enabled) scratchpad past retention -- silently blocking the entire prune
    the moment any scratchpad in the batch had an embedding, since the whole
    call was wrapped in one transaction. Discovered when the maintenance
    scheduler ran for the first time in production."""
    from datetime import timedelta

    record, _ = write_memory(
        content="old scratchpad with an embedding",
        memory_class="scratchpad",
        scope="agent:testagent",
    )
    record_id = record["id"]

    old_created_at = (utc_now() - timedelta(days=30)).isoformat()
    with get_db() as conn:
        conn.execute(
            "UPDATE memory_records SET created_at = ? WHERE id = ?",
            (old_created_at, record_id),
        )
        conn.execute(
            "INSERT INTO memory_embeddings (record_id, vector) VALUES (?, ?)",
            (record_id, b"\x00" * 8),
        )
        conn.commit()

    result = run_scheduled_maintenance()
    assert result["scratchpad_pruned"] >= 1

    with get_db() as conn:
        record_row = conn.execute(
            "SELECT id FROM memory_records WHERE id = ?", (record_id,)
        ).fetchone()
        embedding_row = conn.execute(
            "SELECT record_id FROM memory_embeddings WHERE record_id = ?", (record_id,)
        ).fetchone()
    assert record_row is None, "expired scratchpad should be pruned"
    assert embedding_row is None, "its embedding row must be cleared too"


def test_maintenance_purges_retracted_record_past_grace_period(clean_db):
    from app.services.memory_service import retract_memory
    from datetime import timedelta

    record, _ = write_memory(
        content="retracted long ago, past its grace period",
        memory_class="fact",
        scope="agent:testagent",
    )
    record_id = record["id"]
    assert retract_memory(record_id) is True

    old_status_changed_at = (utc_now() - timedelta(days=60)).isoformat()
    with get_db() as conn:
        conn.execute(
            "UPDATE memory_records SET status_changed_at = ? WHERE id = ?",
            (old_status_changed_at, record_id),
        )
        conn.execute(
            "INSERT INTO memory_embeddings (record_id, vector) VALUES (?, ?)",
            (record_id, b"\x00" * 8),
        )
        conn.commit()

    result = run_scheduled_maintenance()
    assert result["retracted_purged"] >= 1

    with get_db() as conn:
        record_row = conn.execute(
            "SELECT id FROM memory_records WHERE id = ?", (record_id,)
        ).fetchone()
        embedding_row = conn.execute(
            "SELECT record_id FROM memory_embeddings WHERE record_id = ?", (record_id,)
        ).fetchone()
    assert record_row is None, "retracted record past its grace period should be purged"
    assert embedding_row is None, "its embedding row must be cleared too"


def test_maintenance_does_not_purge_retracted_record_within_grace_period(clean_db):
    from app.services.memory_service import retract_memory

    record, _ = write_memory(
        content="retracted moments ago, still within grace period",
        memory_class="fact",
        scope="agent:testagent",
    )
    record_id = record["id"]
    assert retract_memory(record_id) is True

    result = run_scheduled_maintenance()
    assert result["retracted_purged"] == 0

    with get_db() as conn:
        row = conn.execute(
            "SELECT record_status FROM memory_records WHERE id = ?", (record_id,)
        ).fetchone()
    assert row is not None
    assert row["record_status"] == "retracted"


def test_maintenance_respects_configured_retracted_retention_days(clean_db):
    from app.services.memory_service import retract_memory
    from datetime import timedelta

    record, _ = write_memory(
        content="retracted 10 days ago",
        memory_class="fact",
        scope="agent:testagent",
    )
    record_id = record["id"]
    assert retract_memory(record_id) is True

    ten_days_ago = (utc_now() - timedelta(days=10)).isoformat()
    with get_db() as conn:
        conn.execute(
            "UPDATE memory_records SET status_changed_at = ? WHERE id = ?",
            (ten_days_ago, record_id),
        )
        conn.commit()
    system_settings_service.write_raw({"retracted_retention_days": "5"})

    result = run_scheduled_maintenance()
    assert result["retracted_purged"] >= 1

    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM memory_records WHERE id = ?", (record_id,)
        ).fetchone()
    assert row is None


def test_maintenance_lock_second_acquire_fails_while_held(clean_db):
    assert try_acquire_maintenance_lock(lease_seconds=120) is True
    # Simulates a second uvicorn worker's scheduler tick firing moments later:
    # it must not also run the sweep while the first worker's lease is active.
    assert try_acquire_maintenance_lock(lease_seconds=120) is False


def test_maintenance_lock_reacquirable_after_lease_expires(clean_db):
    assert try_acquire_maintenance_lock(lease_seconds=0) is True
    import time

    time.sleep(0.01)
    assert try_acquire_maintenance_lock(lease_seconds=120) is True


def test_expires_at_via_rest_api(test_client, agent_token):
    future = _future_iso()
    r = test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "content": "rest api expires_at test",
            "memory_class": "scratchpad",
            "scope": "agent:testagent",
            "expires_at": future,
        },
    )
    assert r.status_code == 201, r.json()
    record = r.json()["data"]["record"]
    assert record.get("expires_at") is not None


def test_maintenance_keeps_superseded_history_but_purges_old_retractions(clean_db):
    from datetime import timedelta

    from app.database import get_db
    from app.services import backup_service, memory_service
    from app.time_utils import utc_now

    old, _ = memory_service.write_memory("Old value.", "fact", "workspace:hist", topic="h")
    memory_service.write_memory("New value.", "fact", "workspace:hist", topic="h", supersedes_id=old["id"])
    wrong, _ = memory_service.write_memory("Wrong.", "fact", "workspace:hist", topic="w")
    memory_service.retract_memory(wrong["id"])
    long_ago = (utc_now() - timedelta(days=400)).isoformat()
    with get_db() as conn:
        conn.execute("UPDATE memory_records SET status_changed_at = ? WHERE id IN (?, ?)",
                     (long_ago, old["id"], wrong["id"]))
        conn.commit()

    backup_service.run_scheduled_maintenance()

    assert memory_service.get_memory_record(old["id"])["record_status"] == "superseded"
    assert memory_service.get_memory_record(wrong["id"]) is None


def _d(v):
    import json
    return json.loads(v) if isinstance(v, str) else v


def test_an_expiry_and_a_purge_name_the_records_they_removed(clean_db):
    from app.services import audit_service, memory_service

    expired, _ = write_memory(
        content="a decision the writer gave an expiry date",
        memory_class="decision",
        scope="workspace:trace",
        topic="Trace expiry topic",
        expires_at=_past_iso(),
    )
    gone, _ = write_memory(content="a fact that was retracted long ago", memory_class="fact", scope="workspace:trace", topic="Trace purge topic")
    memory_service.retract_memory(gone["id"], retracted_by="rule:duplicate_cluster")
    with get_db() as conn:
        conn.execute("UPDATE memory_records SET status_changed_at = '2020-01-01T00:00:00+00:00' WHERE id = ?", (gone["id"],))
        conn.commit()
    run_scheduled_maintenance()
    swept = _d(audit_service.query_events(action="memory_ttl_swept", limit=5)[0]["details_json"])
    assert {"id": expired["id"], "topic": "Trace expiry topic", "scope": "workspace:trace", "memory_class": "decision"} in swept["records"]
    entry = [e for e in audit_service.query_events(action="memory_auto_retracted", limit=10) if e["resource_id"] == expired["id"]]
    assert entry and _d(entry[0]["details_json"])["rule"] == "expired"
    purged = _d(audit_service.query_events(action="retracted_records_purged", limit=5)[0]["details_json"])
    assert {"id": gone["id"], "topic": "Trace purge topic", "scope": "workspace:trace", "memory_class": "fact"} in purged["records"]
    assert "a decision the writer gave" not in str(swept) and "retracted long ago" not in str(purged)
