"""The Access page's data: every assignment with when it was last used, approvals, and the Unused list."""

import json

import pytest

from app.database import get_db
from app.services import agent_service, connector_service, credential_service, workspace_service
from app.services.auth_service import create_session, create_user

SCOPE = "workspace:acc"


@pytest.fixture
def world(test_client, clean_db):
    create_user("admin", "admin@test.local", "testpassword123", "Admin", "admin")
    test_client.cookies.set("session_token", create_session("admin", channel="dashboard")["session_id"])
    workspace_service.create_workspace("acc", "Accounts", "admin")
    work = ["memory", "coordination", "connectors_read", "connectors_execute"]
    agent_service.create_agent("asst", "Assistant", "admin", capabilities=[*work, "credentials"], read_scopes=[SCOPE], write_scopes=[])
    agent_service.create_agent("svc", "Service", "admin", kind="service")
    agent_service.create_agent("idle-svc", "Idle service", "admin", kind="service")
    used = credential_service.create_credential(SCOPE, "used-key", "not-shown")
    orphan = credential_service.create_credential(SCOPE, "orphan-key", "not-shown")
    connector_service.create_connector_type(
        connector_type_id="ov-cap", display_name="Overview capability", auth_type="none", provider_type="builtin",
        supported_actions=["ping", "wipe"], backend_type="http",
        backend_json=json.dumps({"type": "http", "base_url": {"from": "config", "field": "base_url"}, "requests": {}}),
    )
    binding = connector_service.create_binding(
        "ov-cap", "Main", SCOPE, credential_id=used["id"], config_json='{"base_url": "http://example.test"}'
    )
    with get_db() as conn:
        conn.execute(
            "INSERT INTO credential_consumer_grants (id, consumer_agent_id, entry_id, created_by, created_at) VALUES ('g1', 'svc', ?, 'admin', '2026-01-01')",
            (used["id"],),
        )
        conn.execute(
            "INSERT INTO connector_executions (id, binding_id, action, result_status, executor_agent_id, executed_at) "
            "VALUES ('x1', ?, 'ping', 'success', 'asst', '2026-11-01 10:00:00')",
            (binding["id"],),
        )
        conn.execute(
            "INSERT INTO audit_log (actor_type, actor_id, action, resource_type, resource_id, result, timestamp) "
            "VALUES ('agent', 'asst', 'credential_reference', 'credential', ?, 'success', '2026-11-02 09:00:00')",
            (used["id"],),
        )
        conn.execute(
            "INSERT INTO audit_log (actor_type, actor_id, action, resource_type, resource_id, result, timestamp) "
            "VALUES ('agent', 'svc', 'credential_consumer_resolved', 'credential', ?, 'success', '2026-11-03 09:00:00')",
            (used["id"],),
        )
        conn.execute("UPDATE agent_keys SET last_used_at = '2026-11-17T00:00:00+00:00' WHERE agent_id = 'svc'")
        conn.commit()
    return {"client": test_client, "used": used, "orphan": orphan, "binding": binding}


def _overview(client):
    r = client.get("/api/access/overview")
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _rows(data, assignment):
    return [r for r in data["rows"] if r["assignment"] == assignment]


def test_every_assignment_kind_is_listed_with_who_and_what(world):
    data = _overview(world["client"])
    assert set(data["assignments"]) == {"capability_credential", "agent_capability", "agent_credential", "service_credential"}
    sign_in = _rows(data, "capability_credential")
    assert [(r["from"]["name"], r["to"]["name"]) for r in sign_in] == [("Main (Overview capability)", "used-key")]
    assert [(r["from"]["name"], r["to"]["name"], r["status"]) for r in _rows(data, "agent_capability")] == [
        ("Assistant", "Main (Overview capability)", "can_use")
    ]
    reach = {(r["from"]["name"], r["to"]["name"]): r for r in _rows(data, "agent_credential")}
    assert set(reach) == {("Assistant", "used-key"), ("Assistant", "orphan-key")}
    assert reach[("Assistant", "used-key")]["detail"] == "through workspace Accounts"
    fetch = _rows(data, "service_credential")
    assert [(r["from"]["name"], r["from"]["type"], r["to"]["name"], r["detail"]) for r in fetch] == [
        ("Service", "service", "used-key", "read only")
    ]


def test_last_used_comes_from_runs_the_audit_log_and_resolutions(world):
    data = _overview(world["client"])
    assert _rows(data, "capability_credential")[0]["last_used"] == "2026-11-01 10:00:00"
    assert _rows(data, "agent_capability")[0]["last_used"] == "2026-11-01 10:00:00"
    reach = {r["to"]["name"]: r for r in _rows(data, "agent_credential")}
    assert reach["used-key"]["last_used"] == "2026-11-02 09:00:00"
    assert reach["orphan-key"]["last_used"] is None
    assert _rows(data, "service_credential")[0]["last_used"] == "2026-11-03 09:00:00"


def test_an_action_that_waits_for_a_person_is_shown_on_the_capability_rows(world):
    from app.services import action_gate_service

    action_gate_service.set_gate("ov-cap", "wipe", enabled=True, actors=action_gate_service.ALL_CALLERS, changed_by="admin")
    data = _overview(world["client"])
    assert _rows(data, "agent_capability")[0]["approval"] == ["wipe"]
    assert _rows(data, "capability_credential")[0]["approval"] == ["wipe"]


def test_the_unused_list_names_what_nothing_uses(world):
    unused = _overview(world["client"])["unused"]
    assert [c["name"] for c in unused["credentials"]] == ["orphan-key"]
    assert [s["name"] for s in unused["services"]] == ["Idle service"]
    assert unused["services"][0]["why"] == "It has never been used."
    assert unused["connections"] == [] and unused["left_behind"] == []


def test_a_credential_an_agent_referenced_recently_is_in_use_even_with_nothing_set_up_to_use_it(world):
    from datetime import datetime, timezone

    recent = credential_service.create_credential(SCOPE, "recent-key", "not-shown")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO audit_log (actor_type, actor_id, action, resource_type, resource_id, result, timestamp) "
            "VALUES ('agent', 'asst', 'credential_reference', 'credential', ?, 'success', ?)",
            (recent["id"], datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
    unused = _overview(world["client"])["unused"]["credentials"]
    assert [c["name"] for c in unused] == ["orphan-key"]
    assert unused[0]["why"].endswith("It has never been used.")
    assert "no agent has used it in 90 days" in unused[0]["why"]
    assert unused[0]["last_referenced"] is None


def test_an_old_reference_does_not_keep_a_credential_off_the_list_and_the_list_says_when(world):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO audit_log (actor_type, actor_id, action, resource_type, resource_id, result, timestamp) "
            "VALUES ('agent', 'asst', 'credential_reference', 'credential', ?, 'success', '2026-01-05 09:00:00')",
            (world["orphan"]["id"],),
        )
        conn.commit()
    unused = _overview(world["client"])["unused"]["credentials"]
    assert [c["name"] for c in unused] == ["orphan-key"]
    assert unused[0]["why"].endswith("Last used on 5 Jan 2026.")


def test_a_connection_nobody_can_use_and_what_a_retired_agent_leaves_behind_are_listed(world):
    client = world["client"]
    connector_service.create_binding("ov-cap", "Nobody", "workspace:empty", config_json='{"base_url": "http://example.test"}')
    agent_service.create_agent("old", "Old worker", "admin", capabilities=["memory"])
    credential_service.create_credential("agent:old", "old-key", "not-shown")
    agent_service.deactivate_agent("old")
    unused = _overview(client)["unused"]
    assert [c["name"] for c in unused["connections"]] == ["Nobody (Overview capability)"]
    assert unused["connections"][0]["why"] == "No agent can use it."
    assert [r["name"] for r in unused["left_behind"]] == ["Old worker"]
    assert "old-key" in unused["left_behind"][0]["why"]


def test_the_overview_is_for_an_admin_session_only_and_shows_no_secret(world, test_client):
    assert "not-shown" not in json.dumps(_overview(world["client"]))
    test_client.cookies.clear()
    assert test_client.get("/api/access/overview").status_code in (401, 403)


def test_a_connection_is_shown_by_its_own_name_and_the_capability_only_when_the_name_does_not_start_with_it(world):
    connector_service.create_binding("ov-cap", "Overview capability for the team", SCOPE, config_json='{"base_url": "http://example.test"}')
    connector_service.create_binding("ov-cap", "OVERVIEW CAPABILITY", SCOPE, config_json='{"base_url": "http://example.test"}')
    connector_service.create_binding("ov-cap", "Read-only overview", SCOPE, config_json='{"base_url": "http://example.test"}')
    connector_service.create_binding("ov-cap", "Team data (read only)", SCOPE, config_json='{"base_url": "http://example.test"}')
    names = {r["to"]["name"] for r in _rows(_overview(world["client"]), "agent_capability")}
    assert "Main (Overview capability)" in names  # nothing of the capability in the name: bracketed
    assert "Overview capability for the team" in names
    assert "OVERVIEW CAPABILITY" in names
    assert "Read-only overview" in names  # holds the capability's first word
    assert "Team data (read only) \u00b7 Overview capability" in names  # no two brackets in a row
    assert not any(", " in n for n in names)


def test_last_used_survives_the_execution_log_being_trimmed(world):
    """Runs older than the execution log's retention are gone from it; the audit log still holds a line for each."""
    with get_db() as conn:
        conn.execute("DELETE FROM connector_executions")
        conn.execute(
            "INSERT INTO audit_log (actor_type, actor_id, action, resource_type, resource_id, result, timestamp) "
            "VALUES ('agent', 'asst', 'connector_action_executed', 'connector_binding', ?, 'success', '2026-10-09 22:32:05')",
            (world["binding"]["id"],),
        )
        conn.commit()
    data = _overview(world["client"])
    assert _rows(data, "capability_credential")[0]["last_used"] == "2026-10-09 22:32:05"
    assert _rows(data, "agent_capability")[0]["last_used"] == "2026-10-09 22:32:05"


def test_the_overview_equals_the_answer_asked_one_credential_and_one_connection_at_a_time(world):
    """The overview builds each agent's context once; asking per credential and per connection must give the same."""
    from app.services import reach_service

    data = _overview(world["client"])
    for entry in credential_service.list_credentials(limit=1000):
        expected = [
            (r["agent_id"], r["reason"], r["status"]) for r in reach_service.agents_reaching_credential(entry)
        ]
        got = [
            (r["from"]["id"], r["detail"], r["status"]) for r in _rows(data, "agent_credential") if r["to"]["id"] == entry["id"]
        ]
        assert sorted(got) == sorted(expected)
    for binding in connector_service.list_bindings():
        expected = [(r["agent_id"], r["reason"], r["status"]) for r in reach_service.agents_using_connection(binding)]
        got = [(r["from"]["id"], r["detail"], r["status"]) for r in _rows(data, "agent_capability") if r["to"]["id"] == binding["id"]]
        assert sorted(got) == sorted(expected)


def test_the_overview_builds_each_agents_context_once(world, monkeypatch):
    from app.services import reach_service

    calls = []
    real = reach_service.build_agent_context
    monkeypatch.setattr(reach_service, "build_agent_context", lambda agent: calls.append(agent["id"]) or real(agent))
    for n in range(5):
        credential_service.create_credential(SCOPE, f"extra-{n}", "not-shown")
    _overview(world["client"])
    assert sorted(calls) == ["asst"]  # one active agent (the services are not agents), once


def _audit_fetch(actor, credential_id, when):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO audit_log (actor_type, actor_id, action, resource_type, resource_id, result, timestamp) "
            "VALUES ('agent', ?, 'credential_consumer_resolved', 'credential', ?, 'success', ?)",
            (actor, credential_id, when),
        )
        conn.commit()


def test_a_service_that_fetched_a_secret_is_never_listed_as_never_used_and_the_table_agrees(world):
    """The idle list and the table's Last used read the same sources (key sign-in and secret fetches)."""
    from datetime import datetime, timedelta, timezone

    recent = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
    _audit_fetch("idle-svc", world["used"]["id"], recent)
    data = _overview(world["client"])
    idle = {s["id"]: s for s in data["unused"]["services"]}
    assert "never" not in idle["idle-svc"]["why"].lower()
    assert idle["idle-svc"]["last_signed_in"] == recent
    assert idle["idle-svc"]["why"] == "It may fetch no secret."


def test_a_service_last_used_long_ago_says_when(world):
    _audit_fetch("idle-svc", world["used"]["id"], "2026-01-05 09:00:00")
    idle = {s["id"]: s for s in _overview(world["client"])["unused"]["services"]}
    assert idle["idle-svc"]["why"] == "It was last used on 5 Jan 2026, more than 90 days ago."
