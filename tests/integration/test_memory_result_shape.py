import json


def _write(test_client, token, content, **extra):
    body = {"content": content, "memory_class": "fact", "scope": "agent:testagent"}
    body.update(extra)
    r = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token}"},
        json={"tool": "memory_write", "params": body},
    )
    assert r.status_code == 201, r.json()
    return r.json()["data"]["record"]


def _search(test_client, token, query, **params):
    r = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token}"},
        json={"tool": "memory_search", "params": {"query": query, **params}},
    )
    assert r.status_code == 200, r.json()
    return r.json()["data"]["records"]


# --- lean results ----------------------------------------------------------

DROPPED_FROM_RESULTS = [
    "provenance_json",
    "event_time",
    "valid_from",
    "valid_to",
    "slot_key",
    "status_changed_at",
    "confidence",
    "importance",
    "domain",
]


def test_search_results_drop_bookkeeping_fields(test_client, agent_token):
    _write(test_client, agent_token, "The ingest route is POST /api/webhook/health.")
    records = _search(test_client, agent_token, "ingest")
    assert records, "expected a hit"
    for field in DROPPED_FROM_RESULTS:
        assert field not in records[0], f"{field} should not be in a search result"


def test_search_results_keep_what_the_caller_acts_on(test_client, agent_token):
    _write(
        test_client,
        agent_token,
        "The ingest route is POST /api/webhook/health.",
        subject_anchor="repo:backend/routers/ingest.py",
    )
    record = _search(test_client, agent_token, "ingest")[0]
    for field in (
        "id",
        "content",
        "memory_class",
        "scope",
        "subject_anchor",
        "created_at",
        "last_confirmed_at",
    ):
        assert field in record, f"{field} must survive the lean projection"
    assert record["content"].startswith("The ingest route")


def test_full_view_still_available(test_client, agent_token):
    _write(test_client, agent_token, "The ingest route is POST /api/webhook/health.")
    record = _search(test_client, agent_token, "ingest", view="full")[0]
    assert "provenance_json" in record
    assert "confidence" in record


def test_lean_results_are_materially_smaller(test_client, agent_token):
    for i in range(5):
        _write(test_client, agent_token, f"Observation number {i} about the ingest route.")
    lean = json.dumps(_search(test_client, agent_token, "ingest"))
    full = json.dumps(_search(test_client, agent_token, "ingest", view="full"))
    assert len(lean) < len(full) * 0.7, f"lean={len(lean)} full={len(full)}"


def test_single_record_fetch_is_unchanged(test_client, agent_token):
    """Trimming search results must not trim inspecting one record."""
    record = _write(test_client, agent_token, "the build server is the home server at 192.0.2.10.")
    r = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"tool": "memory_get", "params": {"scope": "agent:testagent"}},
    )
    assert r.status_code == 200
    fetched = next(x for x in r.json()["data"]["records"] if x["id"] == record["id"])
    assert "provenance_json" in fetched


# --- subject anchor --------------------------------------------------------


def test_anchor_round_trips(test_client, agent_token):
    record = _write(
        test_client,
        agent_token,
        "search_memory ranks exact FTS hits above weak semantic ones.",
        subject_anchor="repo:app/services/memory_service.py",
    )
    assert record["subject_anchor"] == "repo:app/services/memory_service.py"


def test_anchor_shape_is_validated(test_client, agent_token):
    """The shape is required so a verifier can dispatch on it; the vocabulary is
    open, because what settles a record depends on what the record is about."""
    for bad in ("the memory service", "repo:", "Not A Type:x"):
        r = test_client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {agent_token}"},
            json={
                "tool": "memory_write",
                "params": {
                    "content": "Something about the system.",
                    "memory_class": "fact",
                    "scope": "agent:testagent",
                    "subject_anchor": bad,
                },
            },
        )
        assert r.status_code == 400, f"{bad!r} should be rejected, got {r.status_code}"


def test_anchor_is_optional_and_normalised(test_client, agent_token):
    from app.services.memory_service import normalize_subject_anchor

    assert normalize_subject_anchor(None) is None
    assert normalize_subject_anchor("  ") is None
    assert normalize_subject_anchor("none") is None
    assert normalize_subject_anchor(" HOST: 192.0.2.10 ") == "host:192.0.2.10"


def test_search_filters_by_anchor_prefix(test_client, agent_token):
    """'What did we record about this area of the code?' is the question it answers."""
    _write(
        test_client,
        agent_token,
        "Ranking floors exact matches above weak semantic hits in the shared engine.",
        subject_anchor="repo:app/services/memory_service.py",
    )
    _write(
        test_client,
        agent_token,
        "Activity search orders newest-first in the shared engine.",
        subject_anchor="repo:app/services/activity_service.py",
    )
    _write(
        test_client,
        agent_token,
        "The dashboard is served on port 3500 by the shared engine.",
        subject_anchor="host:build-server",
    )

    under_services = _search(
        test_client, agent_token, "shared engine", subject_anchor="repo:app/services"
    )
    assert len(under_services) == 2

    exact = _search(
        test_client,
        agent_token,
        "shared engine",
        subject_anchor="repo:app/services/memory_service.py",
    )
    assert len(exact) == 1

    host = _search(test_client, agent_token, "shared engine", subject_anchor="host:build-server")
    assert len(host) == 1


def test_anchor_survives_the_migration_on_an_existing_database(clean_db):
    from app.database import get_db
    from app.schema import _ensure_memory_metadata_columns

    with get_db() as conn:
        # Indexes reference the column, so they go first — the same state a
        # pre-upgrade database is already in.
        conn.execute("DROP INDEX IF EXISTS idx_memory_subject_anchor")
        conn.execute("DROP INDEX IF EXISTS idx_memory_verification")
        conn.execute("ALTER TABLE memory_records DROP COLUMN subject_anchor")
        conn.commit()
    with get_db() as conn:
        _ensure_memory_metadata_columns(conn)
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(memory_records)").fetchall()
        }
    assert "subject_anchor" in columns
