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
        lambda by, pause_ms=0: (started.append(by) or ({"run_id": "r1", "model": "new-model", "status": "running"}, None)),
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


def test_a_slow_run_keeps_its_lease_across_a_batch(records, monkeypatch):
    """A batch that outlasts the lease must not let a second run start."""
    import time as _time

    monkeypatch.setattr(reembed_service, "LEASE_SECONDS", 1)
    seen = []

    def slow_embed(text):
        _time.sleep(0.6)
        seen.append((reembed_service._claim("intruder")[1], reembed_service.status()["job"]["status"]))
        return _vec(1.0), "ok"

    monkeypatch.setattr(embedding_service, "generate_embedding", slow_embed)
    state, _ = reembed_service._claim("test")

    final = reembed_service.run(state)

    assert final["status"] == "completed" and final["embedded"] == 3  # 1.8 s, past the 1 s lease
    assert len(seen) == 3
    assert all(seen_item == ("REEMBED_ALREADY_RUNNING", "running") for seen_item in seen)


def test_a_stop_request_takes_effect_after_the_current_record(records, monkeypatch):
    def embed_then_cancel(text):
        assert reembed_service.cancel() is True
        return _vec(1.0), "ok"

    monkeypatch.setattr(embedding_service, "generate_embedding", embed_then_cancel)
    final = reembed_service.run(reembed_service._claim("test")[0])

    assert final["status"] == "cancelled" and final["embedded"] == 1


def test_cancel_does_not_clobber_progress_saved_since_it_was_requested(records):
    state, _ = reembed_service._claim("test")
    assert reembed_service.cancel() is True
    state["embedded"] = 5
    assert reembed_service._save_progress(state) is True

    stored = reembed_service.status()["job"]
    assert stored["status"] == "cancelling" and stored["embedded"] == 5


def test_an_optional_pause_follows_each_record(records, monkeypatch):
    pauses = []
    monkeypatch.setattr(reembed_service.time, "sleep", pauses.append)
    state, _ = reembed_service._claim("test", pause_ms=250)

    reembed_service.run(state)

    assert state["pause_ms"] == 250 and pauses == [0.25, 0.25, 0.25]
    assert reembed_service._claim("test", pause_ms=10**9)[0]["pause_ms"] == reembed_service.MAX_PAUSE_MS


def test_start_route_validates_and_passes_the_pause(admin_client, monkeypatch):
    save_vector_settings(enabled=True, model="new-model")
    got = []
    monkeypatch.setattr(
        reembed_service, "start",
        lambda by, pause_ms=0: (got.append(pause_ms) or ({"run_id": "r", "model": "new-model"}, None)),
    )
    assert admin_client.post("/api/dashboard/vector-reembed", json={"pause_ms": 500}).status_code == 200
    assert got == [500]
    r = admin_client.post("/api/dashboard/vector-reembed", json={"pause_ms": 999999})
    assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_PAUSE"
    assert admin_client.post("/api/dashboard/vector-reembed", json={"pause_ms": "soon"}).status_code == 400


class _FakeEmbedResponse:
    status_code = 200

    def __init__(self, dims=4):
        self._dims = dims

    def json(self):
        return {"embedding": [0.1] * self._dims}


def _fake_backend(monkeypatch, seconds, sent=None):
    """httpx.post that reports `seconds` elapsed for the timed (non-warm-up) call."""
    import httpx

    clock = {"t": 0.0}

    def fake_post(url, json=None, headers=None, timeout=None):
        if sent is not None:
            sent.append((url, dict(headers or {}), json["model"]))
        if json["prompt"] != "warm up":
            clock["t"] += seconds
        return _FakeEmbedResponse()

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(reembed_service.time, "monotonic", lambda: clock["t"])


def test_estimate_projects_backfill_and_write_delay(records, monkeypatch):
    _fake_backend(monkeypatch, seconds=11.0)

    est = reembed_service.estimate("big-model")

    assert est["ok"] and est["model"] == "big-model" and est["dimension"] == 4
    assert est["seconds_per_record"] == 11.0
    assert est["records_to_embed"] == 3 and est["backfill_seconds"] == 33
    assert est["write_delay_seconds"] == 22.0
    assert est["level"] == "warning" and "30 s timeout" in est["message"]


@pytest.mark.parametrize("seconds,level", [(0.1, "ok"), (2.0, "notice"), (8.0, "warning"), (31.0, "danger")])
def test_estimate_warning_levels(records, monkeypatch, seconds, level):
    _fake_backend(monkeypatch, seconds=seconds)
    assert reembed_service.estimate()["level"] == level


def test_estimate_never_sends_record_content(records, monkeypatch):
    """The sample is synthetic, as long as an average active record, for any URL."""
    sent = []
    _fake_backend(monkeypatch, seconds=1.0, sent=sent)
    prompts = []
    import httpx

    real_post = httpx.post

    def capture(url, json=None, headers=None, timeout=None):
        prompts.append((url, json["prompt"]))
        return real_post(url, json=json, headers=headers, timeout=timeout)

    monkeypatch.setattr(httpx, "post", capture)

    est = reembed_service.estimate(url="http://unsaved.example:11434")

    contents = ["deploy uses docker compose", "the ingest route is a POST", "an older statement"]
    average = (len(contents[0]) + len(contents[1])) // 2
    assert est["sample_chars"] == average
    assert prompts and all(u.startswith("http://unsaved.example") for u, _ in prompts)
    for _, prompt in prompts:
        assert not any(c in prompt for c in contents)


def test_estimate_keeps_the_stored_key_on_the_saved_endpoint(records, monkeypatch):
    save_vector_settings(url="http://embed.saved:11434", auth_type="bearer", api_key="stored-key")
    sent = []
    _fake_backend(monkeypatch, seconds=1.0, sent=sent)

    reembed_service.estimate(url="http://embed.saved:11434")
    reembed_service.estimate(url="http://somewhere.else:11434")

    saved = [h for u, h, _ in sent if u.startswith("http://embed.saved")]
    other = [h for u, h, _ in sent if u.startswith("http://somewhere.else")]
    assert saved and all(h.get("Authorization") == "Bearer stored-key" for h in saved)
    assert other and all("Authorization" not in h for h in other)


def test_estimate_reports_an_unreachable_backend(records, monkeypatch):
    import httpx

    def refuse(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "post", refuse)
    est = reembed_service.estimate()
    assert est["ok"] is False and "refused" in est["error"]


def test_estimate_route(admin_client, monkeypatch):
    seen = []
    monkeypatch.setattr(reembed_service, "estimate",
                        lambda model, url, *rest: seen.append((model, url)) or {"ok": True, "level": "ok"})
    r = admin_client.post("/api/dashboard/vector-reembed/estimate",
                          json={"model": "qwen3-embedding:4b", "url": "http://ollama:11434"})
    assert r.status_code == 200 and seen == [("qwen3-embedding:4b", "http://ollama:11434")]
    assert admin_client.post("/api/dashboard/vector-reembed/estimate", json={"url": "file:///etc"}).status_code == 400
    assert admin_client.post("/api/dashboard/vector-reembed/estimate", json={"model": ""}).status_code == 400

    monkeypatch.setattr(reembed_service, "estimate", lambda *a: {"ok": False, "error": "down"})
    r = admin_client.post("/api/dashboard/vector-reembed/estimate")
    assert r.status_code == 502 and r.json()["error"]["code"] == "ESTIMATE_FAILED"
