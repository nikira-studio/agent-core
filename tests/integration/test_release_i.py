"""Release I: a service cannot own a connection; the credential dialog says what adding a credential replaces."""

import json

import pytest

from app.services import agent_service, connector_service, credential_service, workspace_service
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
        connector_type_id="i-cap", display_name="Release I capability", auth_type="none", provider_type="builtin",
        supported_actions=["ping"], backend_type="http",
        backend_json=json.dumps({"type": "http", "base_url": {"from": "config", "field": "base_url"}, "requests": {}}),
    )
    return {"client": test_client, "headers": csrf_headers(test_client)}


def _body(scope, name="Main"):
    return {"connector_type_id": "i-cap", "name": name, "scope": scope, "config_json": '{"base_url": "http://example.test"}'}


def test_a_service_cannot_own_a_new_connection(world):
    r = world["client"].post("/api/connector-bindings", json=_body("agent:svc"), headers=world["headers"])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "SERVICE_CANNOT_OWN"
    assert "service cannot own" in r.json()["error"]["message"].lower()
    assert connector_service.list_bindings() == []


def test_an_agent_a_workspace_and_a_person_can_still_own_one(world):
    for scope in ("agent:asst", "workspace:acc", "user:admin"):
        r = world["client"].post("/api/connector-bindings", json=_body(scope, name=scope), headers=world["headers"])
        assert r.status_code == 201, r.text


def test_a_connection_cannot_be_given_to_a_service_later(world):
    made = world["client"].post("/api/connector-bindings", json=_body("workspace:acc"), headers=world["headers"]).json()["data"]["binding"]
    r = world["client"].put(f"/api/connector-bindings/{made['id']}", json={"scope": "agent:svc"}, headers=world["headers"])
    assert r.status_code == 400 and r.json()["error"]["code"] == "SERVICE_CANNOT_OWN"
    assert connector_service.get_binding(made["id"])["scope"] == "workspace:acc"
    same = world["client"].put(f"/api/connector-bindings/{made['id']}", json={"scope": "workspace:acc", "name": "Renamed"}, headers=world["headers"])
    assert same.status_code == 200


def test_the_credential_dialog_data_says_what_each_connection_signs_in_with(world):
    mine = credential_service.create_credential("workspace:acc", "mine", "v")
    theirs = credential_service.create_credential("workspace:acc", "theirs", "v")
    with_one = connector_service.create_binding("i-cap", "Has one", "workspace:acc", credential_id=theirs["id"], config_json='{"base_url": "http://example.test"}')
    without = connector_service.create_binding("i-cap", "Has none", "workspace:acc", config_json='{"base_url": "http://example.test"}')
    data = world["client"].get(f"/api/access/credentials/{mine['id']}").json()["data"]
    to_add = {c["id"]: c for c in data["connections_to_add"]}
    assert to_add[with_one["id"]]["signs_in_with"] == {"id": theirs["id"], "name": "theirs"}
    assert to_add[without["id"]]["signs_in_with"] is None


def test_the_credential_dialog_asks_before_replacing_one(world):
    source = world["client"].get("/static/js/credentials.js").text
    start = source.index("async function addAccessConnection")
    body = source[start:source.index("async function removeAccessConnection")]
    assert body.index("signs_in_with") < body.index("method: 'PUT'")
    assert "confirmDialog" in body and "Replace credential" in body
