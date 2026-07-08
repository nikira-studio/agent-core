from app.services.memory_service import write_memory, search_memory
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
    results, _ = search_memory("expired should not appear", authorized_scopes=["agent:testagent"])
    assert not any("expired" in r["content"] for r in results)


def test_non_expired_record_appears_in_search(clean_db):
    write_memory(
        content="this record has a future expiry and should appear",
        memory_class="fact",
        scope="agent:testagent",
        expires_at=_future_iso(),
    )
    results, _ = search_memory("future expiry should appear", authorized_scopes=["agent:testagent"])
    assert any("future expiry" in r["content"] for r in results)


def test_record_without_expires_at_appears_normally(clean_db):
    write_memory(
        content="no expiry set on this record",
        memory_class="fact",
        scope="agent:testagent",
    )
    results, _ = search_memory("no expiry set", authorized_scopes=["agent:testagent"])
    assert any("no expiry set" in r["content"] for r in results)


def test_ttl_sweep_deletes_expired_records_and_embeddings(clean_db):
    record, _ = write_memory(
        content="sweep target record",
        memory_class="scratchpad",
        scope="agent:testagent",
        expires_at=_past_iso(),
    )
    record_id = record["id"]

    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM memory_records WHERE id = ?", (record_id,)
        ).fetchone()
    assert row is not None, "Record should exist before sweep"

    result = run_scheduled_maintenance()
    assert result["ttl_swept"] >= 1

    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM memory_records WHERE id = ?", (record_id,)
        ).fetchone()
    assert row is None, "Record should be deleted after sweep"

    with get_db() as conn:
        embedding = conn.execute(
            "SELECT record_id FROM memory_embeddings WHERE record_id = ?", (record_id,)
        ).fetchone()
    assert embedding is None, "Embedding should be deleted after sweep"


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
        conn.execute(
            "INSERT INTO system_settings (key, value) VALUES ('retracted_retention_days', '5')"
        )
        conn.commit()

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
