"""Credentials owned by a connection: no agent can reach them, the hub still uses them, a person who manages the
connection can manage them, the set-up is all-or-nothing, and deleting a connection has two defaults."""

import json

import pytest

from app.database import get_db
from app.security.context import build_user_context
from app.security.scope_enforcer import ScopeEnforcer, build_agent_context
from app.services import (
    agent_service, broker_service, connector_service, credential_service, reach_service, workspace_service,
)
from app.services.auth_service import create_session, create_user
from tests.conftest import csrf_headers

WORK = ["memory", "coordination", "credentials", "connectors_read", "connectors_execute"]


@pytest.fixture
def world(test_client, clean_db):
    create_user("admin", "admin@test.local", "testpassword123", "Admin", "admin")
    test_client.cookies.set("session_token", create_session("admin", channel="dashboard")["session_id"])
    workspace_service.create_workspace("acc", "Accounts", "admin")
    agent_service.create_agent("asst", "Assistant", "admin", capabilities=WORK, read_scopes=["workspace:acc"], write_scopes=[])
    agent_service.create_agent("svc", "Service", "admin", kind="service")
    connector_service.create_connector_type(
        connector_type_id="own-cap", display_name="Owned capability", auth_type="none", provider_type="builtin",
        supported_actions=["ping"], backend_type="http",
        backend_json=json.dumps({"type": "http", "base_url": {"from": "config", "field": "base_url"}, "requests": {}}),
    )
    return {"client": test_client, "headers": csrf_headers(test_client)}


def _create(world, owned_by="connection", name="Main", scope="workspace:acc", credential_name="its-key"):
    body = {
        "connector_type_id": "own-cap", "name": name, "scope": scope, "config_json": '{"base_url": "http://example.test"}',
        "new_credential": {"name": credential_name, "value": "the-secret-value", "owned_by": owned_by},
    }
    return world["client"].post("/api/connector-bindings", json=body, headers=world["headers"])


def _helper(client, entry, agent_id):
    broker_service.ensure_broker_credential()
    plaintext = broker_service._read_broker_credential()
    return client.post(
        "/internal/credentials/resolve", headers={"Authorization": f"Broker {plaintext}"},
        json={"variable_name": entry["reference_name"], "agent_id": agent_id},
    )


def test_a_new_credential_made_with_a_connection_belongs_to_it_and_the_hub_still_uses_it(world):
    made = _create(world)
    assert made.status_code == 201, made.text
    binding = made.json()["data"]["binding"]
    entry = credential_service.get_credential(binding["credential_id"])
    assert entry["scope"] == f"capability:{binding['id']}"
    assert connector_service.get_binding_with_credential(binding["id"])["credential_plaintext"] == "the-secret-value"
    with get_db() as conn:
        audited = conn.execute("SELECT details_json FROM audit_log WHERE action = 'credential_entry_created'").fetchone()
    assert json.loads(audited["details_json"])["scope"] == entry["scope"]


def test_no_agent_can_reach_it_by_any_door(world):
    entry = credential_service.get_credential(_create(world).json()["data"]["binding"]["credential_id"])
    agent = agent_service.get_agent_by_id("asst")
    context = build_agent_context(agent)
    enforcer = ScopeEnforcer(context.read_scopes, context.write_scopes, context.agent_id, active_workspace_ids=context.active_workspace_ids)
    assert not enforcer.can_read(entry["scope"]) and not enforcer.can_write(entry["scope"])
    assert reach_service.agents_reaching_credential(entry) == []
    refused = _helper(world["client"], entry, "asst")
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "SCOPE_DENIED"
    assert "the-secret-value" not in refused.text
    summary = reach_service.agent_summary(agent)
    assert entry["id"] not in {c["id"] for c in summary["credentials"]}


def test_an_agent_can_never_be_given_a_connections_own_scope(world):
    client, headers = world["client"], world["headers"]
    binding = _create(world).json()["data"]["binding"]
    owner = f"capability:{binding['id']}"
    created = client.post("/api/agents", json={"id": "greedy", "display_name": "Greedy", "read_scopes": [owner]}, headers=headers)
    assert created.status_code == 400 and created.json()["error"]["code"] == "INVALID_SCOPE"
    updated = client.put("/api/agents/asst", json={"write_scopes": [owner]}, headers=headers)
    assert updated.status_code == 400 and updated.json()["error"]["code"] == "INVALID_SCOPE"
    with pytest.raises(ValueError):
        agent_service.update_agent("asst", read_scopes=["workspace:acc", owner])
    # even if one were stored by other means, it is never part of the agent's effective scopes
    with get_db() as conn:
        conn.execute("UPDATE agents SET read_scopes_json = ? WHERE id = 'asst'", (json.dumps(["workspace:acc", owner]),))
        conn.commit()
    context = build_agent_context(agent_service.get_agent_by_id("asst"))
    assert owner not in context.read_scopes and owner not in context.write_scopes


def test_a_person_who_manages_the_connection_can_manage_its_credential_and_nobody_else_can(world):
    create_user("pat", "pat@test.local", "testpassword123", "Pat", "user")
    create_user("lee", "lee@test.local", "testpassword123", "Lee", "user")
    workspace_service.create_workspace("pats", "Pat's place", "pat")
    binding = connector_service.create_binding("own-cap", "Pat's", "workspace:pats", config_json='{"base_url": "http://example.test"}')
    entry = credential_service.create_credential(f"capability:{binding['id']}", "pat-key", "not-shown")
    pat = ScopeEnforcer(**_person(build_user_context({"user_id": "pat", "role": "user"})))
    lee = ScopeEnforcer(**_person(build_user_context({"user_id": "lee", "role": "user"})))
    assert pat.can_read(entry["scope"]) and pat.can_write(entry["scope"])
    assert not lee.can_read(entry["scope"]) and not lee.can_write(entry["scope"])
    assert not pat.can_read("capability:no-such-connection")


def _person(context):
    return {
        "read_scopes": context.read_scopes, "write_scopes": context.write_scopes, "agent_id": None,
        "is_admin": context.is_admin, "active_workspace_ids": context.active_workspace_ids,
    }


def test_the_checkbox_keeps_the_old_behaviour_the_credential_is_stored_under_the_owner(world):
    binding = _create(world, owned_by="owner", credential_name="shared-key").json()["data"]["binding"]
    entry = credential_service.get_credential(binding["credential_id"])
    assert entry["scope"] == "workspace:acc"
    assert [a["agent_id"] for a in reach_service.agents_reaching_credential(entry)] == ["asst"]


def test_the_set_up_is_all_or_nothing(world, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(credential_service, "create_credential", boom)
    with pytest.raises(RuntimeError):
        _create(world)
    assert connector_service.list_bindings() == []  # no half-made connection
    monkeypatch.undo()
    assert _create(world, name="", credential_name=" ").status_code == 400
    again = _create(world)
    assert again.status_code == 201
    duplicate = _create(world, owned_by="owner", name="Second", credential_name="same")
    assert duplicate.status_code == 201
    clash = _create(world, owned_by="owner", name="Third", credential_name="same")
    assert clash.status_code == 409 and clash.json()["error"]["code"] == "DUPLICATE_CREDENTIAL"
    assert {b["name"] for b in connector_service.list_bindings()} == {"Main", "Second"}  # the third left nothing behind


def test_an_agent_cannot_store_a_credential_with_a_connection(test_client, admin_token, agent_token):
    connector_service.create_connector_type(
        connector_type_id="own-cap2", display_name="Owned capability 2", auth_type="none", provider_type="builtin",
        supported_actions=["ping"], backend_type="http",
        backend_json=json.dumps({"type": "http", "base_url": {"from": "config", "field": "base_url"}, "requests": {}}),
    )
    body = {
        "connector_type_id": "own-cap2", "name": "Mine", "scope": "agent:testagent",
        "config_json": '{"base_url": "http://example.test"}', "new_credential": {"name": "k", "value": "v"},
    }
    refused = test_client.post("/api/connector-bindings", json=body, headers={"Authorization": f"Bearer {agent_token}"})
    assert refused.status_code == 403


def test_moving_a_credential_into_a_connection_is_refused_when_another_uses_it_and_allowed_otherwise(world):
    client, headers = world["client"], world["headers"]
    entry = credential_service.create_credential("workspace:acc", "moving", "not-shown")
    first = connector_service.create_binding("own-cap", "First", "workspace:acc", credential_id=entry["id"], config_json='{"base_url": "http://example.test"}')
    second = connector_service.create_binding("own-cap", "Second", "workspace:acc", credential_id=entry["id"], config_json='{"base_url": "http://example.test"}')
    shared = client.put(f"/api/credentials/entries/{entry['id']}", json={"scope": f"capability:{first['id']}"}, headers=headers)
    assert shared.status_code == 409 and shared.json()["error"]["code"] == "SHARED_CREDENTIAL"
    connector_service.update_binding(second["id"], credential_id="")
    ok = client.put(f"/api/credentials/entries/{entry['id']}", json={"scope": f"capability:{first['id']}"}, headers=headers)
    assert ok.status_code == 200, ok.text
    assert credential_service.get_credential(entry["id"])["scope"] == f"capability:{first['id']}"
    assert client.put(f"/api/credentials/entries/{entry['id']}", json={"scope": "capability:nowhere"}, headers=headers).status_code == 400
    # and out again: agents that read the workspace reach it once more
    back = client.put(f"/api/credentials/entries/{entry['id']}", json={"scope": "workspace:acc"}, headers=headers)
    assert back.status_code == 200
    assert [a["agent_id"] for a in reach_service.agents_reaching_credential(credential_service.get_credential(entry["id"]))] == ["asst"]


def test_a_service_can_still_be_granted_a_connection_owned_credential_and_the_page_says_whose_it_is(world):
    client, headers = world["client"], world["headers"]
    entry_id = _create(world).json()["data"]["binding"]["credential_id"]
    granted = client.post("/api/credential-consumers/grants", json={"consumer_agent_id": "svc", "entry_id": entry_id, "allow_write": False}, headers=headers)
    assert granted.status_code == 201, granted.text
    page = client.get(f"/api/access/credentials/{entry_id}").json()["data"]
    assert page["credential"]["owner"] == "connection Main" and page["agents"] == []
    assert [s["service_id"] for s in page["services"]] == ["svc"]


def test_deleting_a_connection_keeps_its_credential_through_the_api_unless_asked_and_lists_what_is_left(world):
    client, headers = world["client"], world["headers"]
    keep = _create(world, name="Keep me").json()["data"]["binding"]
    drop = _create(world, name="Drop me", credential_name="drop-key").json()["data"]["binding"]
    drop_entry = drop["credential_id"]
    client.post("/api/credential-consumers/grants", json={"consumer_agent_id": "svc", "entry_id": keep["credential_id"], "allow_write": False}, headers=headers)
    info = client.get(f"/api/access/connections/{keep['id']}").json()["data"]["owned_credential"]
    assert info["services_holding"] == ["Service"] and info["name"] == "its-key"
    kept = client.delete(f"/api/connector-bindings/{keep['id']}", headers=headers)
    assert kept.status_code == 200 and kept.json()["data"]["credential_kept"] == ["its-key"]
    assert credential_service.get_credential(keep["credential_id"]) is not None
    left = client.get("/api/access/overview").json()["data"]["unused"]["left_behind"]
    assert [r["name"] for r in left] == ["its-key"] and "Services that still receive it: Service" in left[0]["why"]
    gone = client.delete(f"/api/connector-bindings/{drop['id']}?delete_credential=true", headers=headers)
    assert gone.status_code == 200 and gone.json()["data"]["credential_deleted"] == ["drop-key"]
    assert credential_service.get_credential(drop_entry) is None
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'credential_entry_deleted'").fetchone()[0] == 1
