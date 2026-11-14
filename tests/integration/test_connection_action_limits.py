"""A connection may narrow what its service allows (1.3.1); it can never widen it.

One rule, ``connection_allows``, backs every place an action is run, listed, resolved, summarised or tested. These tests
go through each of those sites, plus "a connection cannot exceed its service".
"""

import json

import pytest

from app.security.exceptions import APIError
from app.services import connector_service as cs
from tests.conftest import csrf_headers
from tests.integration.test_approvals import _agent, _browser_admin, _workspace

SERVICE = "limits-svc"
ACTIONS = [
    {"name": "list_things", "side_effect": "read", "description": "List things", "input_schema": {"type": "object"}},
    {"name": "get_thing", "side_effect": "none", "description": "Get one thing", "input_schema": {"type": "object"}},
    {"name": "create_thing", "side_effect": "write", "description": "Make a thing", "input_schema": {"type": "object"}},
    {"name": "delete_thing", "side_effect": "destructive", "description": "Remove a thing", "input_schema": {"type": "object"}},
]
REQUESTS = {a["name"]: {"method": "GET" if a["side_effect"] in ("read", "none") else "POST", "path": "/" + a["name"]} for a in ACTIONS}


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


class _FakeExecutor:
    needs_session = False

    def __init__(self, calls):
        self.calls = calls

    def execute(self, action, params, credential, config_json, session=None):
        self.calls.append(action)
        return {"success": True, "status": 200, "body": {"ran": action}}

    def test_connection(self, credential, config_json):
        return {"success": True}


@pytest.fixture
def world(test_client, admin_token, monkeypatch):
    _workspace(test_client, admin_token)
    caps = ["memory", "coordination", "connectors_read", "connectors_execute"]
    agent_key = _agent(test_client, admin_token, "advisor", caps)
    cs.create_connector_type(
        connector_type_id=SERVICE, display_name="Limits Service", auth_type="none", provider_type="builtin",
        supported_actions=ACTIONS, backend_type="http",
        backend_json=json.dumps({"type": "http", "base_url": {"from": "config", "field": "base_url"}, "requests": REQUESTS}),
    )
    calls: list = []
    monkeypatch.setattr(cs, "_resolve_executor", lambda ct: _FakeExecutor(calls))

    def binding(name, mode="all", disabled=None, scope="workspace:ops"):
        return cs.create_binding(SERVICE, name, scope, config_json='{"base_url": "http://example.test"}',
                                 actions_mode=mode, disabled_actions=disabled)

    return {"client": test_client, "admin": admin_token, "key": agent_key, "calls": calls, "binding": binding}


def _run(world, binding, action, key=None):
    return world["client"].post("/mcp", headers=_bearer(key or world["key"]),
                                json={"tool": "connectors_run", "params": {"binding_id": binding["id"], "action": action, "params": {}}})


# ---- the rule itself ------------------------------------------------------------------------------------------

def test_the_rule_for_each_mode_and_a_connection_cannot_exceed_its_service():
    ct = {"disabled_actions": ["delete_thing"], "supported_actions": ACTIONS,
          "backend_type": "http", "backend_json": json.dumps({"requests": REQUESTS})}
    everything = {"actions_mode": "all"}
    read_only = {"actions_mode": "read_only"}
    custom = {"actions_mode": "custom", "disabled_actions": ["create_thing"]}
    assert cs.effective_actions(ct, everything) == ["list_things", "get_thing", "create_thing"]  # the service's switch wins
    assert cs.effective_actions(ct, read_only) == ["list_things", "get_thing"]
    assert cs.effective_actions(ct, custom) == ["list_things", "get_thing"]
    # a connection that lists nothing as off still cannot turn on what the service turned off
    assert cs.connection_allows(ct, {"actions_mode": "custom", "disabled_actions": []}, "delete_thing") is False
    assert cs.connection_allows(ct, everything, "delete_thing") is False
    # an unknown mode is never a way to widen; an action the service does not have is never allowed by a mode
    assert cs.connection_allows(ct, {"actions_mode": "bogus"}, "list_things") is False
    assert cs.connection_allows(ct, {"actions_mode": "read_only"}, "no_such_action") is False


def test_side_effects_come_from_what_each_kind_of_service_declares():
    mcp = {"provider_type": "mcp", "tool_snapshot_json": json.dumps({"tools": [
        {"name": "look", "raw": {"name": "look", "annotations": {"readOnlyHint": True}}},
        {"name": "change", "raw": {"name": "change", "annotations": {"readOnlyHint": False}}},
        {"name": "silent", "raw": {"name": "silent"}},
        {"name": "lies", "raw": {"name": "lies", "annotations": {"readOnlyHint": True, "destructiveHint": True}}},
    ]})}
    assert [cs.action_side_effect(mcp, n) for n in ("look", "change", "silent", "lies")] == ["read", "write", "unknown", "write"]
    spec = {"provider_type": "openapi", "operations_json": json.dumps({"operations": [
        {"operation_id": "getX", "method": "GET"}, {"operation_id": "postX", "method": "POST"}]})}
    assert (cs.action_side_effect(spec, "getX"), cs.action_side_effect(spec, "postX")) == ("read", "write")
    web = {"provider_type": "builtin", "backend_type": "generic_http"}
    assert cs.action_side_effect(web, "GET /status") == "read" and cs.action_side_effect(web, "DELETE /x") == "unknown"
    assert cs.action_side_effect(web, "call_endpoint") == "unknown"  # no way to know what it will send
    adapter = {"supported_actions": ACTIONS}
    assert [cs.action_side_effect(adapter, a["name"]) for a in ACTIONS] == ["read", "read", "write", "write"]


def test_a_tool_that_appears_later_is_off_for_read_only_unless_it_says_it_is_read_only_and_on_for_the_rest():
    def mcp(tools):
        return {"provider_type": "mcp", "tool_snapshot_json": json.dumps({"tools": tools})}

    before = mcp([{"name": "look", "raw": {"annotations": {"readOnlyHint": True}}}])
    after = mcp([{"name": "look", "raw": {"annotations": {"readOnlyHint": True}}},
                 {"name": "new_write", "raw": {"annotations": {"readOnlyHint": False}}},
                 {"name": "new_unknown", "raw": {}},
                 {"name": "new_read", "raw": {"annotations": {"readOnlyHint": True}}}])
    read_only = {"actions_mode": "read_only"}
    assert cs.effective_actions(before, read_only) == ["look"]
    assert cs.effective_actions(after, read_only) == ["look", "new_read"]
    assert cs.effective_actions(after, {"actions_mode": "all"}) == ["look", "new_write", "new_unknown", "new_read"]
    assert cs.effective_actions(after, {"actions_mode": "custom", "disabled_actions": ["look"]}) == ["new_write", "new_unknown", "new_read"]


# ---- run ---------------------------------------------------------------------------------------------------------

def test_run_refuses_what_a_read_only_connection_does_not_allow_and_never_sends_it(world):
    ro = world["binding"]("Read only", "read_only")
    assert _run(world, ro, "list_things").json()["data"]["success"] is True
    refused = _run(world, ro, "create_thing").json()
    assert refused["ok"] is False and refused["error"]["code"] == "CONNECTION_DISABLED_ACTION"
    assert "read-only" in refused["error"]["message"] and "create_thing" in refused["error"]["message"]
    assert world["calls"] == ["list_things"]  # the refused action never reached the service


def test_run_honours_a_custom_list_and_an_unlimited_connection_still_runs_everything(world):
    custom = world["binding"]("Custom", "custom", ["delete_thing"])
    assert _run(world, custom, "create_thing").json()["ok"] is True
    assert _run(world, custom, "delete_thing").json()["error"]["code"] == "CONNECTION_DISABLED_ACTION"
    plain = world["binding"]("Everything")
    assert _run(world, plain, "delete_thing").json()["ok"] is True


def test_the_service_switch_still_wins_over_every_mode_with_its_own_message(world):
    world["binding"]("Anything")  # make sure a connection exists
    cs.update_connector_type_actions(SERVICE, ["create_thing"])
    plain = world["binding"]("Plain")
    custom_none_off = world["binding"]("Custom none off", "custom", [])
    for b in (plain, custom_none_off):
        r = _run(world, b, "create_thing").json()
        assert r["ok"] is False and r["error"]["code"] == "DISABLED_ACTION"  # the service's own refusal, unchanged
        assert cs.execute_binding_action(b["id"], "create_thing", {})["error_code"] == "DISABLED_ACTION"


def test_the_inner_run_function_refuses_too_so_no_path_can_skip_the_check(world):
    ro = world["binding"]("Read only", "read_only")
    inner = cs.execute_binding_action(ro["id"], "create_thing", {})
    assert inner["success"] is False and inner["error_code"] == "CONNECTION_DISABLED_ACTION"
    assert world["calls"] == []


def test_a_narrowed_action_is_refused_before_anything_is_held_for_a_person(world):
    from app.services import action_gate_service

    ro = world["binding"]("Read only", "read_only")
    client = world["client"]
    _browser_admin(client)
    r = client.put(f"/api/connector-types/{SERVICE}/gates/create_thing", headers=csrf_headers(client),
                   json={"enabled": True, "actors": "all"})
    assert r.status_code == 200, r.text
    client.cookies.clear()
    assert action_gate_service.gate_for(SERVICE, "create_thing", "agent", "advisor") is not None
    out = _run(world, ro, "create_thing").json()
    assert out["error"]["code"] == "CONNECTION_DISABLED_ACTION"  # not "waiting for approval"
    from app.database import get_db

    with get_db() as conn:
        held = conn.execute("SELECT COUNT(*) FROM connector_run_approvals").fetchone()[0] if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'connector_run_approvals'").fetchone() else 0
    assert held == 0


def test_rest_run_refuses_the_same_way(world):
    ro = world["binding"]("Read only", "read_only")
    r = world["client"].post(f"/api/connector-bindings/{ro['id']}/run", headers=_bearer(world["key"]),
                             json={"action": "delete_thing", "params": {}})
    assert r.status_code == 200 and r.json()["data"]["result"]["error_code"] == "CONNECTION_DISABLED_ACTION"
    assert world["calls"] == []


# ---- resolve ------------------------------------------------------------------------------------------------------

def test_resolve_skips_a_connection_that_does_not_allow_the_action(world):
    from app.security.effective_authority import system_authority

    ro = world["binding"]("Read only", "read_only")
    full = world["binding"]("Full")
    authority = system_authority("test resolve")
    assert cs.resolve_authorized_binding(authority, connector_type_id=SERVICE, action="create_thing")["id"] == full["id"]
    cs.update_binding(full["id"], enabled=False)
    with pytest.raises(APIError):  # only the read-only connection is left, and it does not allow a write
        cs.resolve_authorized_binding(authority, connector_type_id=SERVICE, action="create_thing")
    assert cs.resolve_authorized_binding(authority, connector_type_id=SERVICE, action="list_things")["id"] == ro["id"]


# ---- what an agent is shown ---------------------------------------------------------------------------------------

def _actions(world, params, key=None):
    r = world["client"].post("/mcp", headers=_bearer(key or world["key"]),
                             json={"tool": "connectors_actions_list", "params": {"connector_type_id": SERVICE, **params}})
    assert r.status_code == 200, r.text
    return r.json()["data"]


def test_an_agent_only_sees_what_its_connection_allows(world):
    ro = world["binding"]("Read only", "read_only")
    assert _actions(world, {})["actions"] == ["list_things", "get_thing"]
    assert _actions(world, {"binding_id": ro["id"]})["actions"] == ["list_things", "get_thing"]
    # with a second, unlimited connection of its own, the agent can run everything, so it is shown everything
    world["binding"]("Full")
    assert len(_actions(world, {})["actions"]) == 4
    assert _actions(world, {"binding_id": ro["id"]})["actions"] == ["list_things", "get_thing"]


def test_an_admin_still_sees_the_service_level_catalogue_and_the_rest_listing_follows_the_connection(world):
    ro = world["binding"]("Read only", "read_only")
    admin = world["client"].get(f"/api/connector-types/{SERVICE}/tools?include_disabled=true",
                                headers=_bearer(world["admin"])).json()["data"]
    assert [t["action"] for t in admin["tools"]] == [a["name"] for a in ACTIONS]  # the service's own view, for its switches
    one = world["client"].get(f"/api/connector-bindings/{ro['id']}/tools", headers=_bearer(world["key"])).json()["data"]
    assert [t["action"] for t in one["tools"]] == ["list_things", "get_thing"]
    scoped = world["client"].get(f"/api/connector-types/{SERVICE}/tools?binding_id={ro['id']}", headers=_bearer(world["key"])).json()["data"]
    assert [t["action"] for t in scoped["tools"]] == ["list_things", "get_thing"]


def test_the_summary_gives_each_connection_its_own_actions_and_a_connection_with_none_is_not_usable(world):
    ro = world["binding"]("Read only", "read_only")
    nothing = world["binding"]("Nothing", "custom", [a["name"] for a in ACTIONS])
    r = world["client"].post("/mcp", headers=_bearer(world["key"]),
                             json={"tool": "connectors_summary", "params": {"connector_type_id": SERVICE, "view": "full"}})
    by_id = {b["id"]: b for c in r.json()["data"]["connectors"] for b in c["bindings"]}
    assert by_id[ro["id"]]["actions"] == {"mode": "read_only", "allowed": 2, "total": 4}
    assert by_id[nothing["id"]]["actions"] == {"mode": "custom", "allowed": 0, "total": 4}
    assert by_id[ro["id"]]["usable_by_caller"] is True and by_id[nothing["id"]]["usable_by_caller"] is False
    compact = world["client"].post("/mcp", headers=_bearer(world["key"]),
                                   json={"tool": "connectors_summary", "params": {}}).json()["data"]
    row = next(b for c in compact["connectors"] for b in c["bindings"] if b["id"] == ro["id"])
    assert row["actions"] == "read_only (2/4)"


# ---- the connection test ------------------------------------------------------------------------------------------

def test_the_connection_test_only_considers_actions_the_connection_allows():
    from app.connectors.http_engine import HttpEngine

    ct = {"supported_actions": ACTIONS, "backend_json": json.dumps({"test_action": "list_things", "requests": REQUESTS})}
    binding = {"actions_mode": "custom", "disabled_actions": ["list_things"]}
    narrowed = dict(ct, disabled_actions=cs.connection_disabled_actions(ct, binding))
    assert HttpEngine(ct).pick_test_action({}) == "list_things"
    assert HttpEngine(narrowed).pick_test_action({}) == "get_thing"  # the named test action is off here: the next read one


# ---- setting it: API, audit, who may --------------------------------------------------------------------------------

def test_a_person_sets_the_mode_through_the_api_and_it_is_validated_and_audited(world):
    from app.database import get_db

    client, admin = world["client"], world["admin"]
    created = client.post("/api/connector-bindings", headers=_bearer(admin), json={
        "connector_type_id": SERVICE, "name": "By hand", "scope": "workspace:ops",
        "config_json": '{"base_url": "http://example.test"}', "actions_mode": "read_only"})
    assert created.status_code == 201, created.text
    binding = created.json()["data"]["binding"]
    assert binding["actions_mode"] == "read_only" and binding["disabled_actions"] == []
    updated = client.put(f"/api/connector-bindings/{binding['id']}", headers=_bearer(admin),
                         json={"actions_mode": "custom", "disabled_actions": ["delete_thing"]})
    assert updated.status_code == 200, updated.text
    assert updated.json()["data"]["binding"]["disabled_actions"] == ["delete_thing"]
    assert client.put(f"/api/connector-bindings/{binding['id']}", headers=_bearer(admin),
                      json={"actions_mode": "everything"}).status_code == 400
    assert client.put(f"/api/connector-bindings/{binding['id']}", headers=_bearer(admin),
                      json={"actions_mode": "custom", "disabled_actions": ["not_an_action"]}).status_code == 400
    with get_db() as conn:
        events = [dict(r) for r in conn.execute(
            "SELECT action, details_json FROM audit_log WHERE action = 'connector_binding_actions_updated'").fetchall()]
    assert events and "custom" in events[0]["details_json"]


def test_an_agent_cannot_choose_what_a_connection_allows_and_a_connection_it_makes_is_read_only(world):
    client = world["client"]
    refused = client.post("/api/connector-bindings", headers=_bearer(world["key"]), json={
        "connector_type_id": SERVICE, "name": "Agent made", "scope": "workspace:ops",
        "config_json": '{"base_url": "http://example.test"}', "actions_mode": "all"})
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "ACTIONS_NOT_FOR_AGENTS"
    made = client.post("/api/connector-bindings", headers=_bearer(world["key"]), json={
        "connector_type_id": SERVICE, "name": "Agent made", "scope": "workspace:ops",
        "config_json": '{"base_url": "http://example.test"}'})
    assert made.status_code == 201, made.text
    assert made.json()["data"]["binding"]["actions_mode"] == "read_only"  # creating one is never a way round a narrower one
    ro = world["binding"]("Read only", "read_only")
    widen = client.put(f"/api/connector-bindings/{ro['id']}", headers=_bearer(world["key"]), json={"actions_mode": "all"})
    assert widen.status_code == 403
    assert cs.get_binding(ro["id"])["actions_mode"] == "read_only"


def test_existing_connections_inherit_everything(world):
    plain = world["binding"]("Plain")
    assert plain["actions_mode"] == "all" and plain["disabled_actions"] == []
    assert cs.effective_actions(cs.get_connector_type(SERVICE), plain) == [a["name"] for a in ACTIONS]


# ---- the page --------------------------------------------------------------------------------------------------------

def test_the_dialogs_offer_the_choice_and_the_table_says_what_a_connection_allows(world):
    from app.services.auth_service import create_session

    ro = world["binding"]("Read only", "read_only")
    custom = world["binding"]("Chosen", "custom", ["delete_thing"])
    world["binding"]("Everything")
    client = world["client"]
    client.cookies.set("session_token", create_session("admin")["session_id"])
    page = client.get("/connectors").text
    for prefix in ("binding", "edit-binding"):
        assert f'id="{prefix}-actions-group"' in page
    assert "Everything the service allows" in page and "Read-only" in page and "Choose actions" in page
    assert "Read-only &middot; 2 of 4 actions" in page and "Chosen actions &middot; 3 of 4 actions" in page
    assert page.count("Everything the service allows") == 2  # the label text; the plain connection adds no note
