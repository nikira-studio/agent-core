"""The reverse of the citation a record already carries.

A record has cited the activity that produced it since provenance was
introduced. Traversing the other way — "what did that session conclude?" — is
what a handoff actually needs, and there was no way to ask it.
"""

from app.database import get_db
from app.services import activity_service, memory_service


def _open_activity(test_client, token, description="Investigate the ranking bug"):
    r = test_client.post(
        "/api/activity",
        headers={"Authorization": f"Bearer {token}"},
        json={"task_description": description, "memory_scope": "agent:testagent"},
    )
    assert r.status_code == 201, r.json()
    return r.json()["data"]["activity"]["id"]


def _write(test_client, token, content, scope="agent:testagent"):
    r = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "tool": "memory_write",
            "params": {"content": content, "memory_class": "fact", "scope": scope},
        },
    )
    assert r.status_code == 201, r.json()
    return r.json()["data"]["record"]["id"]


def test_an_activity_reports_what_it_produced(test_client, agent_token):
    activity_id = _open_activity(test_client, agent_token)
    first = _write(test_client, agent_token, "Ranking floors exact matches above weak hits.")
    second = _write(test_client, agent_token, "The floor is applied before the freshness bonus.")

    r = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"tool": "activity_get", "params": {"activity_id": activity_id}},
    )
    assert r.status_code == 200, r.json()
    produced = r.json()["data"]["produced_records"]
    assert [p["id"] for p in produced] == [first, second]
    # Lean projection, same as any other search result.
    assert "provenance_json" not in produced[0]
    assert produced[0]["content"].startswith("Ranking floors")


def test_records_written_outside_the_activity_are_not_attributed(test_client, agent_token):
    """Only work done while the activity was open belongs to it."""
    orphan = _write(test_client, agent_token, "Written with no activity open.")
    activity_id = _open_activity(test_client, agent_token)
    during = _write(test_client, agent_token, "Written while the activity was open.")

    assert [r["id"] for r in memory_service.records_for_activity(activity_id)] == [during]
    assert orphan not in [r["id"] for r in memory_service.records_for_activity(activity_id)]


def test_search_can_filter_by_the_activity_that_produced_a_record(test_client, agent_token):
    activity_id = _open_activity(test_client, agent_token)
    produced = _write(test_client, agent_token, "Shared token: written during the task.")
    activity_service.update_activity(activity_id, status="completed", task_result="done")
    other = _write(test_client, agent_token, "Shared token: written afterwards.")

    r = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "tool": "memory_search",
            "params": {"query": "shared token", "activity_id": activity_id},
        },
    )
    assert r.status_code == 200, r.json()
    ids = [rec["id"] for rec in r.json()["data"]["records"]]
    assert ids == [produced]
    assert other not in ids


def test_produced_records_respect_scope(test_client, agent_token, admin_token):
    """Producing a record does not entitle a later reader to see it."""
    activity_id = _open_activity(test_client, agent_token)
    _write(test_client, agent_token, "Visible to the writing agent.")

    record, _ = memory_service.write_memory(
        content="Written into a scope the agent cannot read.",
        memory_class="fact",
        scope="user:admin",
        provenance_json=memory_service.build_provenance(
            actor_type="agent",
            actor_id="testagent",
            channel="mcp",
            source_kind="agent_inference",
            extras={"activity_id": activity_id},
        ),
    )

    unfiltered = memory_service.records_for_activity(activity_id)
    assert record["id"] in [r["id"] for r in unfiltered]

    scoped = memory_service.records_for_activity(
        activity_id, authorized_scopes=["agent:testagent"]
    )
    assert record["id"] not in [r["id"] for r in scoped]


def test_no_readable_scope_returns_nothing(test_client, agent_token):
    activity_id = _open_activity(test_client, agent_token)
    _write(test_client, agent_token, "Something.")
    assert memory_service.records_for_activity(activity_id, authorized_scopes=[]) == []


def test_retracted_records_are_left_out_unless_asked_for(test_client, agent_token):
    activity_id = _open_activity(test_client, agent_token)
    record_id = _write(test_client, agent_token, "A conclusion later withdrawn.")
    memory_service.retract_memory(record_id)

    assert memory_service.records_for_activity(activity_id) == []
    assert [
        r["id"]
        for r in memory_service.records_for_activity(activity_id, include_inactive=True)
    ] == [record_id]


def test_a_handoff_briefing_says_what_the_session_concluded(test_client, agent_token):
    """The point of the feature: a briefing that carries findings, not just intent."""
    from app.services import briefing_service

    activity_id = _open_activity(test_client, agent_token, "Diagnose the failing deploy")
    _write(test_client, agent_token, "Root cause: the runner image lacked libpq-dev.")
    activity_service.update_activity(
        activity_id, status="completed", task_result="Diagnosed and fixed."
    )

    briefing = briefing_service.generate_handoff_briefing(
        activity_id,
        requesting_agent_id="testagent",
        requesting_user_id="admin",
        authorized_scopes=["agent:testagent"],
        is_admin=True,
    )
    contents = [r["content"] for r in briefing["produced_records"]]
    assert any("libpq-dev" in c for c in contents)


def test_the_lookup_uses_the_index(clean_db):
    """A JSON scan over every record would not survive a large corpus."""
    with get_db() as conn:
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM memory_records "
            "WHERE json_extract(provenance_json, '$.activity_id') = ?",
            ("x",),
        ).fetchall()
    assert any("idx_memory_source_activity" in " ".join(str(c) for c in row) for row in plan), plan
