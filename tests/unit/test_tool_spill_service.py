from app.database import get_db
from app.services import tool_spill_service


def test_spill_and_fetch_roundtrip(clean_db):
    content = "A" * 10000
    res = tool_spill_service.spill("agent:a", "memory_get", content, ttl_hours=24)
    assert res["offloaded"] is True
    assert res["total_chars"] == 10000
    assert res["handle"]
    assert res["summary"]["preview_truncated"] is True

    first = tool_spill_service.fetch(res["handle"], 0, 4000, agent_id="agent:a")
    assert first["returned_chars"] == 4000
    assert first["has_more"] is True
    assert first["next_offset"] == 4000

    # Page to the end and confirm full reconstruction.
    chunks = []
    offset = 0
    while True:
        page = tool_spill_service.fetch(res["handle"], offset, 4000, agent_id="agent:a")
        chunks.append(page["content"])
        if not page["has_more"]:
            break
        offset = page["next_offset"]
    assert "".join(chunks) == content


def test_summary_includes_json_structure(clean_db):
    content = '{"records": [1, 2, 3], "total": 3}'
    res = tool_spill_service.spill("agent:a", "t", content, ttl_hours=24)
    assert res["summary"]["structure"] == "object"
    assert "records" in res["summary"]["top_level_keys"]


def test_fetch_scoped_to_agent(clean_db):
    res = tool_spill_service.spill("agent:a", "t", "data" * 100, ttl_hours=24)
    assert tool_spill_service.fetch(res["handle"], 0, 100, agent_id="agent:b") is None
    assert tool_spill_service.fetch(res["handle"], 0, 100, agent_id="agent:a") is not None


def test_fetch_missing_handle_returns_none(clean_db):
    assert tool_spill_service.fetch("does-not-exist", 0, 100) is None


def test_cleanup_expired(clean_db):
    res = tool_spill_service.spill("agent:a", "t", "x" * 100, ttl_hours=24)
    with get_db() as conn:
        conn.execute(
            "UPDATE tool_result_spill SET expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", res["handle"]),
        )
    removed = tool_spill_service.cleanup_expired()
    assert removed == 1
    assert tool_spill_service.fetch(res["handle"], 0, 10) is None
