"""Re-embedding memory records after the embedding model changes."""

import json
import struct
from datetime import datetime, timedelta, timezone

import pytest

from app.database import get_db
from app.services import embedding_service, reembed_service, system_settings_service
from app.services.vector_settings_service import save_vector_settings

SCOPE = "workspace:reembed-test"


def _vec(*values):
    return struct.pack(f"{len(values)}f", *values)


def _record(content, status="active"):
    from app.services.memory_service import write_memory

    result = write_memory(content=content, memory_class="fact", scope=SCOPE)
    record = result[0] if isinstance(result, tuple) else result
    record_id = record["id"]
    if status != "active":
        with get_db() as conn:
            conn.execute("UPDATE memory_records SET record_status = ? WHERE id = ?", (status, record_id))
            conn.commit()
    return record_id


def _store(record_id, model):
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO memory_embeddings (record_id, vector, model) VALUES (?, ?, ?)",
            (record_id, _vec(1.0, 0.0), model),
        )
        conn.commit()


def _vector_model(record_id):
    with get_db() as conn:
        row = conn.execute("SELECT model FROM memory_embeddings WHERE record_id = ?", (record_id,)).fetchone()
    return row["model"] if row else None


@pytest.fixture
def records(clean_db, monkeypatch):
    """Three records written with vector search off, one carrying an old-model vector."""
    save_vector_settings(enabled=False)
    ids = {
        "active_old": _record("deploy uses docker compose"),
        "active_none": _record("the ingest route is a POST"),
        "superseded": _record("an older statement", status="superseded"),
    }
    _store(ids["active_old"], "old-model")
    save_vector_settings(enabled=True, model="new-model")
    calls = []

    def fake_embed(text):
        calls.append(text)
        return _vec(0.5, 0.5), "ok"

    monkeypatch.setattr(embedding_service, "generate_embedding", fake_embed)
    return ids, calls


def test_coverage_counts_only_vectors_from_the_current_model(records):
    ids, _ = records
    cov = reembed_service.coverage()
    assert cov == {
        "model": "new-model", "active_total": 2, "active_embedded": 0, "active_missing": 2,
        "total": 3, "embedded": 0, "missing": 3,
    }
    assert reembed_service.coverage("old-model")["active_embedded"] == 1


def test_a_run_embeds_every_record_active_first_and_replaces_old_vectors(records):
    ids, calls = records
    state, err = reembed_service._claim("test")
    assert err is None

    final = reembed_service.run(state)

    assert final["status"] == "completed" and final["embedded"] == 3 and final["failed"] == 0
    assert calls[-1] == "an older statement"  # superseded records come after active ones
    assert {_vector_model(i) for i in ids.values()} == {"new-model"}
    assert reembed_service.coverage()["missing"] == 0
    assert reembed_service.status()["job"]["status"] == "completed"


def test_a_second_run_skips_records_already_done(records):
    _, calls = records
    reembed_service.run(reembed_service._claim("test")[0])
    calls.clear()

    final = reembed_service.run(reembed_service._claim("test")[0])

    assert final["embedded"] == 0 and calls == []


def test_repeated_failures_stop_the_run(records, monkeypatch):
    monkeypatch.setattr(reembed_service, "MAX_CONSECUTIVE_FAILURES", 2)
    monkeypatch.setattr(embedding_service, "generate_embedding", lambda text: (None, "ollama_error:404"))

    final = reembed_service.run(reembed_service._claim("test")[0])

    assert final["status"] == "failed" and final["failed"] == 2
    assert "ollama_error:404" in final["error"]


def test_a_failed_record_is_skipped_and_the_rest_still_embedded(records, monkeypatch):
    def flaky(text):
        return (None, "exception:Timeout") if text == "the ingest route is a POST" else (_vec(1.0), "ok")

    monkeypatch.setattr(embedding_service, "generate_embedding", flaky)
    final = reembed_service.run(reembed_service._claim("test")[0])

    assert final["status"] == "completed" and final["embedded"] == 2 and final["failed"] == 1


def test_only_one_run_at_a_time_and_a_dead_run_can_be_taken_over(records):
    state, err = reembed_service._claim("test")
    assert err is None
    assert reembed_service._claim("test") == (None, "REEMBED_ALREADY_RUNNING")

    stale = dict(state, lease_at=(datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat())
    system_settings_service.write_raw({reembed_service.STATE_KEY: json.dumps(stale)})
    assert reembed_service.status()["job"]["status"] == "interrupted"

    taken, err = reembed_service._claim("test")
    assert err is None and taken["run_id"] != state["run_id"]
    # The old run notices it no longer owns the job and leaves the new state alone.
    reembed_service.run(state)
    assert reembed_service.status()["job"]["run_id"] == taken["run_id"]


def test_cancel_stops_the_run(records):
    state, _ = reembed_service._claim("test")
    assert reembed_service.cancel() is True

    final = reembed_service.run(state)

    assert final["status"] == "cancelled" and final["embedded"] == 0
    assert reembed_service.cancel() is False


def test_a_model_change_during_the_run_stops_it(records):
    state, _ = reembed_service._claim("test")
    save_vector_settings(model="another-model")

    final = reembed_service.run(state)

    assert final["status"] == "failed" and "model changed" in final["error"]


def test_nothing_starts_while_vector_search_is_off(records):
    save_vector_settings(enabled=False)
    assert reembed_service._claim("test") == (None, "VECTOR_SEARCH_DISABLED")


@pytest.fixture
def admin_client(test_client, clean_db):
    from app.services.auth_service import create_session, create_user

    create_user("admin", "admin@test.local", "testpassword123", "Admin", "admin")
    test_client.cookies.set("session_token", create_session("admin", channel="dashboard")["session_id"])
    return test_client


def test_routes_report_and_start_a_run(admin_client, monkeypatch):
    save_vector_settings(enabled=True, model="new-model")
    _record("a record to embed")
    started = []
    monkeypatch.setattr(
        reembed_service, "start",
        lambda by: (started.append(by) or ({"run_id": "r1", "model": "new-model", "status": "running"}, None)),
    )

    r = admin_client.get("/api/dashboard/vector-reembed")
    assert r.status_code == 200
    assert r.json()["data"]["coverage"]["missing"] == 1

    r = admin_client.post("/api/dashboard/vector-reembed")
    assert r.status_code == 200 and r.json()["data"]["job"]["run_id"] == "r1"
    assert started == ["user:admin"]

    assert admin_client.post("/api/dashboard/vector-reembed/cancel").status_code == 409


def test_start_route_refuses_while_vector_search_is_off(admin_client):
    save_vector_settings(enabled=False)
    r = admin_client.post("/api/dashboard/vector-reembed")
    assert r.status_code == 400 and r.json()["error"]["code"] == "VECTOR_SEARCH_DISABLED"


def test_routes_are_admin_only(test_client, clean_db):
    from app.services.auth_service import create_session, create_user

    create_user("someone", "u@test.local", "testpassword123", "User", "user")
    test_client.cookies.set("session_token", create_session("someone", channel="dashboard")["session_id"])
    assert test_client.get("/api/dashboard/vector-reembed").status_code in (401, 403)
    assert test_client.post("/api/dashboard/vector-reembed").status_code in (401, 403)


def test_settings_page_shows_the_reembed_controls(admin_client):
    html = admin_client.get("/settings").text
    assert 'id="vector-reembed-coverage"' in html
    assert "window.startReembed = startReembed" in html
