"""Two clocks, not one.

`created_at` and `status_changed_at` say when the system learned something.
`valid_from` and `valid_to` say when it was true. Keeping them apart is what
lets the corpus answer "what was true in March" rather than only "what do we
believe now" — and without it, an old fact and the fact that replaced it look
equally current to anything reading history.
"""

from app.database import get_db
from app.services import memory_service

SCOPE = "workspace:proj"


def _write(content, **extra):
    record, _ = memory_service.write_memory(
        content=content, memory_class=extra.pop("memory_class", "fact"), scope=SCOPE, **extra
    )
    return record


def _backdate(record_id, created_at, valid_from=None):
    with get_db() as conn:
        conn.execute(
            "UPDATE memory_records SET created_at = ?, valid_from = COALESCE(?, valid_from) "
            "WHERE id = ?",
            (created_at, valid_from, record_id),
        )
        conn.commit()


def _search(query, **kwargs):
    records, _ = memory_service.search_memory(query, [SCOPE], **kwargs)
    return [r["id"] for r in records]


# --- closing the window ----------------------------------------------------


def test_superseding_closes_the_old_record_s_validity(clean_db):
    old = _write("The service runs in Seattle.")
    assert memory_service.get_memory_record(old["id"])["valid_to"] is None

    new = _write("The service runs in New York.", supersedes_id=old["id"])

    closed = memory_service.get_memory_record(old["id"])
    assert closed["record_status"] == "superseded"
    assert closed["valid_to"] is not None, "the old fact stopped being true"
    assert memory_service.get_memory_record(new["id"])["valid_to"] is None


def test_an_explicit_end_date_is_not_overwritten(clean_db):
    """The writer knows something the supersession does not."""
    old = _write("The contract runs through Q2.", valid_to="2026-06-30T00:00:00+00:00")
    _write("The contract was renewed through Q4.", supersedes_id=old["id"])

    assert memory_service.get_memory_record(old["id"])["valid_to"].startswith("2026-06-30")


def test_moving_a_record_does_not_end_its_validity(clean_db):
    """Relocating a record changes where it lives, not whether it is true."""
    record = _write("The ingest route is POST /api/webhook/health.")
    moved, error = memory_service.move_memory(record["id"], "workspace:other")

    assert error is None, error
    assert memory_service.get_memory_record(record["id"])["valid_to"] is None


def test_retracting_does_not_end_validity_either(clean_db):
    """Retraction says the record was not worth standing behind, which is a
    different statement from "it stopped being true"."""
    record = _write("A record later withdrawn.")
    memory_service.retract_memory(record["id"])
    assert memory_service.get_memory_record(record["id"])["valid_to"] is None


# --- asking about a moment -------------------------------------------------


def test_the_canonical_case(clean_db):
    """Stated in 2024, changed in 2026. Both must not look equally current."""
    old = _write("The user lives in Seattle.")
    _backdate(old["id"], "2024-03-01T00:00:00+00:00")
    new = _write(
        "The user lives in New York.",
        supersedes_id=old["id"],
        valid_from="2026-01-15T00:00:00+00:00",
    )

    assert _search("user lives") == [new["id"]], "current state is the new record only"
    assert _search("user lives", as_of="2025-01-01") == [old["id"]], "in 2025 it was Seattle"
    assert _search("user lives", as_of="2026-06-01") == [new["id"]], "by June it was New York"
    # Stating when the new fact took effect is what closed the old window there.
    assert memory_service.get_memory_record(old["id"])["valid_to"].startswith("2026-01-15")


def test_records_written_later_do_not_leak_into_the_past(clean_db):
    """Absent an explicit valid_from, a record is taken to start when it was
    written — otherwise every new record would claim to have always been true."""
    record = _write("A conclusion reached this week.")
    assert _search("conclusion", as_of="2020-01-01") == []
    assert _search("conclusion") == [record["id"]]


def test_a_backdated_claim_is_honoured(clean_db):
    """A record can describe a period earlier than when it was written."""
    record = _write(
        "The pricing tier changed at the start of the year.",
        valid_from="2026-01-01T00:00:00+00:00",
    )
    assert _search("pricing tier", as_of="2026-02-01") == [record["id"]]
    assert _search("pricing tier", as_of="2025-06-01") == []


def test_a_point_in_time_query_ignores_retracted_records(clean_db):
    old = _write("Something we later withdrew entirely.")
    _backdate(old["id"], "2026-01-01T00:00:00+00:00")
    memory_service.retract_memory(old["id"])
    assert _search("withdrew", as_of="2026-02-01") == []


def test_a_malformed_as_of_is_rejected(clean_db):
    _write("Anything.")
    try:
        memory_service.search_memory("anything", [SCOPE], as_of="not-a-date")
        raise AssertionError("expected a refusal")
    except ValueError as exc:
        assert "as_of" in str(exc)


# --- over the wire ---------------------------------------------------------


def test_point_in_time_search_over_mcp(test_client, agent_token):
    def write(content, **params):
        r = test_client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {agent_token}"},
            json={
                "tool": "memory_write",
                "params": {
                    "content": content,
                    "memory_class": "fact",
                    "scope": "agent:testagent",
                    **params,
                },
            },
        )
        assert r.status_code == 201, r.json()
        return r.json()["data"]["record"]["id"]

    old = write("The deployment target is the staging cluster.")
    _backdate(old, "2026-02-01T00:00:00+00:00")
    new = write("The deployment target is the production cluster.", supersedes_id=old)

    def search(**params):
        r = test_client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {agent_token}"},
            json={"tool": "memory_search", "params": {"query": "deployment target", **params}},
        )
        assert r.status_code == 200, r.json()
        return [rec["id"] for rec in r.json()["data"]["records"]]

    assert search() == [new]
    assert search(as_of="2026-02-15") == [old]


def test_an_unusable_as_of_is_a_clean_error(test_client, agent_token):
    r = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"tool": "memory_search", "params": {"query": "anything", "as_of": "March"}},
    )
    assert r.status_code == 400, r.text[:200]
    assert r.json()["error"]["code"] == "INVALID_INPUT"


def test_the_rest_route_rejects_it_the_same_way(test_client, agent_token):
    """Both transports reach the same service, so both must fail the same way."""
    r = test_client.post(
        "/api/memory/search",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"query": "anything", "as_of": "March"},
    )
    assert r.status_code == 400, r.text[:200]
    assert r.json()["error"]["code"] == "INVALID_INPUT"
