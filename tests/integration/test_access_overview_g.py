"""The Access page's data: every assignment with when it was last used, approvals, and the Unused list."""

import json

import pytest

from app.database import get_db
from app.services import agent_service, connector_service, credential_service, workspace_service
from app.services.auth_service import create_session, create_user
from tests.conftest import csrf_headers

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
    assert [(r["from"]["name"], r["to"]["name"]) for r in sign_in] == [("Overview capability, Main", "used-key")]
    assert [(r["from"]["name"], r["to"]["name"], r["status"]) for r in _rows(data, "agent_capability")] == [
        ("Assistant", "Overview capability, Main", "can_use")
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
    assert unused["services"][0]["why"] == "It has never signed in."
    assert unused["connections"] == [] and unused["left_behind"] == []


def test_a_connection_nobody_can_use_and_what_a_retired_agent_leaves_behind_are_listed(world):
    client = world["client"]
    connector_service.create_binding("ov-cap", "Nobody", "workspace:empty", config_json='{"base_url": "http://example.test"}')
    agent_service.create_agent("old", "Old worker", "admin", capabilities=["memory"])
    credential_service.create_credential("agent:old", "old-key", "not-shown")
    agent_service.deactivate_agent("old")
    unused = _overview(client)["unused"]
    assert [c["name"] for c in unused["connections"]] == ["Overview capability, Nobody"]
    assert unused["connections"][0]["why"] == "No agent can use it."
    assert [r["name"] for r in unused["left_behind"]] == ["Old worker"]
    assert "old-key" in unused["left_behind"][0]["why"]


def test_the_overview_is_for_an_admin_session_only_and_shows_no_secret(world, test_client):
    assert "not-shown" not in json.dumps(_overview(world["client"]))
    test_client.cookies.clear()
    assert test_client.get("/api/access/overview").status_code in (401, 403)
