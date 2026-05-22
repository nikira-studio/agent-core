from app.services.memory_service import write_memory, search_memory
from app.services.backup_service import run_scheduled_maintenance
from app.database import get_db


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
