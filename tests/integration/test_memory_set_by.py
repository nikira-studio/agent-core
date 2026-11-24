"""A record shows whether a person or an agent set that version. An agent's replacement of a person's record
is the agent's own version: the label does not carry over, and it points back at the person's version."""

from app.database import get_db
from app.services import activity_service, agent_service, audit_service, memory_service, workspace_service

SCOPE = "workspace:setby-test"


def _setup():
    with get_db() as conn:
        owner = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
    workspace_service.create_workspace("setby-test", "Set By Test", owner)
    _, issued = agent_service.create_agent(
        agent_id="setby-agent", display_name="Set By Agent", owner_user_id=owner,
        read_scopes=[SCOPE], write_scopes=[SCOPE],
    )
    return {"Authorization": f"Bearer {issued}"}, owner


def _person_record(content="Use Postgres for the ledger.", topic="ledger-db"):
    record, _ = memory_service.write_memory(
        content, "decision", SCOPE, topic=topic, source_kind="human_direct",
        provenance_json=memory_service.build_provenance(
            actor_type="user", actor_id="alex", channel="api", source_kind="human_direct", scope=SCOPE
        ),
    )
    return record


def _tool(client, headers, tool, **params):
    response = client.post("/mcp", headers=headers, json={"tool": tool, "params": params})
    return response


def _get(client, headers, record_id, view=None):
    params = {"ids": [record_id]}
    if view:
        params["view"] = view
    body = _tool(client, headers, "memory_get", **params).json()
    return body["data"]["records"][0]


def test_a_person_written_record_is_shown_as_person_set(test_client, admin_token):
    headers, _ = _setup()
    record = _person_record()
    assert _get(test_client, headers, record["id"])["set_by"] == "person"
    assert _get(test_client, headers, record["id"], "full")["set_by"] == "person"


def test_an_agent_replacing_a_person_record_writes_its_own_version(test_client, admin_token):
    headers, _ = _setup()
    original = _person_record()
    response = _tool(
        test_client, headers, "memory_write",
        content="Use SQLite for the ledger; Postgres was dropped.", memory_class="decision",
        scope=SCOPE, topic="ledger-db", supersedes_id=original["id"],
    )
    assert response.status_code == 201, response.json()
    new_id = response.json()["data"]["record"]["id"]

    new = _get(test_client, headers, new_id)
    assert new["set_by"] == "agent:setby-agent"
    assert new["replaces_person_set"] == original["id"]
    stored = memory_service.get_memory_record(new_id)
    assert stored["source_kind"] == "agent_inference"  # the label did not carry over

    # The person's version is kept and readable, still labelled as theirs.
    old = _get(test_client, headers, original["id"])
    assert old["set_by"] == "person" and old["record_status"] == "superseded"

    found = _tool(test_client, headers, "memory_search", query="ledger", scope=SCOPE).json()["data"]["records"]
    by_id = {r["id"]: r for r in found}
    assert by_id[new_id]["set_by"] == "agent:setby-agent" and by_id[new_id]["replaces_person_set"] == original["id"]


def test_an_agent_cannot_claim_a_person_set_kind(test_client, admin_token):
    headers, _ = _setup()
    response = _tool(
        test_client, headers, "memory_write", content="Alex chose X.", memory_class="decision",
        scope=SCOPE, source_kind="human_direct",
    )
    assert response.status_code == 403 and response.json()["error"]["code"] == "SOURCE_KIND_DENIED"


def test_replacing_an_agent_record_carries_no_person_pointer(test_client, admin_token):
    headers, _ = _setup()
    first = _tool(test_client, headers, "memory_write", content="Pin the image.", memory_class="fact", scope=SCOPE)
    first_id = first.json()["data"]["record"]["id"]
    second = _tool(
        test_client, headers, "memory_write", content="Pin the image by digest.", memory_class="fact",
        scope=SCOPE, supersedes_id=first_id,
    )
    new = _get(test_client, headers, second.json()["data"]["record"]["id"])
    assert new["set_by"] == "agent:setby-agent" and "replaces_person_set" not in new


def test_a_person_replacing_a_person_record_stays_person_set(test_client, admin_token):
    headers, _ = _setup()
    original = _person_record()
    newer, _ = memory_service.write_memory(
        "Use Postgres 16 for the ledger.", "decision", SCOPE, topic="ledger-db", source_kind="human_direct",
        supersedes_id=original["id"],
        provenance_json=memory_service.build_provenance(
            actor_type="user", actor_id="alex", channel="api", source_kind="human_direct", scope=SCOPE
        ),
    )
    got = _get(test_client, headers, newer["id"])
    assert got["set_by"] == "person" and "replaces_person_set" not in got


def test_the_compact_survey_and_the_session_show_who_set_a_record(test_client, admin_token):
    headers, owner = _setup()
    execution = test_client.post(
        "/api/session-context", headers=headers, json={"memory_scope": SCOPE, "host_session_ref": "t-1"}
    ).json()["data"]["execution_id"]
    written = _tool(
        test_client, headers, "memory_write", content="The queue is drained nightly.", memory_class="fact",
        scope=SCOPE, execution_id=execution,
    ).json()["data"]["record"]
    surveyed = _tool(test_client, headers, "memory_get", scope=SCOPE, view="compact").json()["data"]["records"]
    assert {r["id"]: r["set_by"] for r in surveyed}[written["id"]] == "agent:setby-agent"
    full = _get(test_client, headers, written["id"], "full")
    assert full["set_by"] == "agent:setby-agent"
    assert full["session"] == execution == memory_service.get_memory_record(written["id"]).get("source_execution_id")


def test_a_retraction_says_who_and_from_which_session(test_client, admin_token):
    headers, owner = _setup()
    execution = test_client.post(
        "/api/session-context", headers=headers, json={"memory_scope": SCOPE, "host_session_ref": "t-2"}
    ).json()["data"]["execution_id"]
    activity_service.create_activity(
        agent_id="setby-agent", user_id=owner, task_description="Tidy the ledger notes",
        memory_scope=SCOPE, source_execution_id=execution,
    )
    original = _person_record()
    response = _tool(test_client, headers, "memory_retract", record_id=original["id"], reason="Superseded by the move.")
    assert response.status_code == 200, response.json()

    trace = memory_service.retraction_traces([original["id"]])[original["id"]]
    assert trace["by"] == "setby-agent" and trace["by_person"] is False
    assert trace["session"] == execution and trace["reason"] == "Superseded by the move."

    # A person's retraction is marked as a person's, with no session.
    other = _person_record("Keep the old export.", "export")
    memory_service.retract_memory(other["id"], retracted_by="alex")
    audit_service.write_event(
        actor_type="user", actor_id="alex", action="memory_retract", resource_type="memory_record",
        resource_id=other["id"], result="success", details=None,
    )
    traced = memory_service.retraction_traces([other["id"]])[other["id"]]
    assert traced["by_person"] is True and traced["session"] is None
