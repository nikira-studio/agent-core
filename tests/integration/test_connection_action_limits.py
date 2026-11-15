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


def test_an_agent_cannot_choose_what_a_connection_allows_and_a_connection_it_makes_allows_nothing_yet(world):
    client = world["client"]
    refused = client.post("/api/connector-bindings", headers=_bearer(world["key"]), json={
        "connector_type_id": SERVICE, "name": "Agent made", "scope": "workspace:ops",
        "config_json": '{"base_url": "http://example.test"}', "actions_mode": "all"})
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "ACTIONS_NOT_FOR_AGENTS"
    made = client.post("/api/connector-bindings", headers=_bearer(world["key"]), json={
        "connector_type_id": SERVICE, "name": "Agent made", "scope": "workspace:ops",
        "config_json": '{"base_url": "http://example.test"}'})
    assert made.status_code == 201, made.text
    assert made.json()["data"]["binding"]["actions_mode"] == "none"  # creating one is never a way round a narrower one
    assert "allows nothing yet because an agent created it" in made.json()["data"]["note"]
    refused_write = world["client"].post("/mcp", headers=_bearer(world["key"]), json={"tool": "connectors_run", "params": {
        "binding_id": made.json()["data"]["binding"]["id"], "action": "create_thing", "params": {}}}).json()
    assert refused_write["error"]["code"] == "CONNECTION_DISABLED_ACTION"
    assert "Ask a person to open Services > Edit connection" in refused_write["error"]["message"]  # says what to do
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


# ---- found by the independent review ----------------------------------------------------------------------------------

def _web_service(world, mode="all", disabled=None, service_disabled=None):
    cs.create_connector_type(connector_type_id="plain-web", display_name="Plain web", auth_type="none",
                             provider_type="builtin", backend_type="generic_http", endpoint_url="https://web.example.test",
                             supported_actions=[], disabled_actions=service_disabled)
    return cs.create_binding("plain-web", "Web", "workspace:ops", config_json='{"base_url": "https://web.example.test"}',
                             actions_mode=mode, disabled_actions=disabled)


def test_a_read_only_web_connection_cannot_be_made_to_send_a_write_by_a_param_default_or_header(monkeypatch):
    from app.connectors.generic_http import GenericHttpConnector

    sent = []
    connector = GenericHttpConnector()
    monkeypatch.setattr(connector, "_call", lambda credential, config, params, session=None: sent.append(dict(params)) or {"success": True})
    connector.execute("GET /thing/1", {"method": "DELETE", "url": "https://other.example/x",
                                       "headers": {"X-HTTP-Method-Override": "DELETE", "Accept": "application/json"}}, None, '{"base_url": "https://web.example.test"}')
    assert sent[0]["method"] == "GET" and sent[0]["path"] == "/thing/1" and "url" not in sent[0]
    assert sent[0]["headers"] == {"Accept": "application/json"}
    connector.execute("DELETE /thing/1", {}, None, '{"base_url": "https://web.example.test"}')
    assert sent[1]["method"] == "DELETE"  # the action is still what decides
    # the generic call_endpoint action is the one that takes its method and path from the caller, and still does
    connector.execute("call_endpoint", {"method": "PUT", "path": "/things/9", "body": {"a": 1}}, None, '{"base_url": "https://web.example.test"}')
    assert sent[2]["method"] == "PUT" and sent[2]["path"] == "/things/9" and sent[2]["body"] == {"a": 1}
    # a bare "GET" with a path param keeps working: the path is the caller's, the method is still the action's
    connector.execute("GET", {"path": "/things/10", "method": "DELETE"}, None, '{"base_url": "https://web.example.test"}')
    assert sent[3]["method"] == "GET" and sent[3]["path"] == "/things/10"


def test_a_web_service_action_is_compared_in_one_spelling_so_respelling_it_does_not_get_round_a_limit(world):
    custom = _web_service(world, "custom", ["DELETE /thing/1"])
    ct = cs.get_connector_type("plain-web")
    for spelling in ("DELETE /thing/1", "delete /thing/1", "DELETE   /thing/1", " DELETE /thing/1", "DELETE\t/thing/1"):
        assert cs.connection_allows(ct, custom, spelling) is False, spelling
        assert cs.execute_binding_action(custom["id"], spelling, {})["error_code"] == "CONNECTION_DISABLED_ACTION", spelling
    assert cs.connection_allows(ct, custom, "GET /thing/1") is True
    # the same for the service's own switch
    cs.update_connector_type_actions("plain-web", ["POST /x"])
    ct = cs.get_connector_type("plain-web")
    plain = cs.create_binding("plain-web", "Web 2", "workspace:ops", config_json='{"base_url": "https://web.example.test"}')
    for spelling in ("POST /x", "post /x", "POST    /x"):
        assert cs.execute_binding_action(plain["id"], spelling, {})["error_code"] == "DISABLED_ACTION", spelling


def test_read_only_on_a_web_service_allows_only_get_and_head_spellings(world):
    ro = _web_service(world, "read_only")
    ct = cs.get_connector_type("plain-web")
    assert cs.connection_allows(ct, ro, "get /x") and cs.connection_allows(ct, ro, "HEAD /x")
    assert not any(cs.connection_allows(ct, ro, a) for a in ("DELETE /x", "post /x", "call_endpoint", "PATCH /x"))


def test_a_connection_an_agent_made_runs_nothing_until_a_person_chooses(world):
    made = world["client"].post("/api/connector-bindings", headers=_bearer(world["key"]), json={
        "connector_type_id": SERVICE, "name": "Agent made", "scope": "workspace:ops",
        "config_json": '{"base_url": "http://example.test"}'}).json()["data"]["binding"]
    # it cannot restore a read action a person turned off on another connection by creating its own
    refused = _run(world, made, "list_things").json()
    assert refused["error"]["code"] == "CONNECTION_DISABLED_ACTION" and "allows nothing yet" in refused["error"]["message"]
    assert _actions(world, {"binding_id": made["id"]})["actions"] == []
    # a person opens it up
    client = world["client"]
    ok = client.put(f"/api/connector-bindings/{made['id']}", headers=_bearer(world["admin"]), json={"actions_mode": "read_only"})
    assert ok.status_code == 200
    assert _run(world, made, "list_things").json()["ok"] is True


def test_an_agent_is_not_told_about_actions_its_connections_do_not_allow_even_when_it_asks(world):
    ro = world["binding"]("Read only", "read_only")
    assert _actions(world, {"include_disabled": True})["actions"] == ["list_things", "get_thing"]
    rest = world["client"].get(f"/api/connector-types/{SERVICE}/tools?include_disabled=true", headers=_bearer(world["key"])).json()["data"]
    assert [t["action"] for t in rest["tools"]] == ["list_things", "get_thing"]
    one = world["client"].get(f"/api/connector-bindings/{ro['id']}/tools?include_disabled=true", headers=_bearer(world["key"])).json()["data"]
    assert [t["action"] for t in one["tools"]] == ["list_things", "get_thing"]


def test_an_unreadable_custom_list_allows_nothing_rather_than_everything(world):
    from app.database import get_db

    custom = world["binding"]("Custom", "custom", ["delete_thing"])
    with get_db() as conn:
        conn.execute("UPDATE connector_bindings SET actions_disabled_json = ? WHERE id = ?", ("{not json", custom["id"]))
        conn.commit()
    broken = cs.get_binding(custom["id"])
    assert broken["actions_corrupt"] is True
    assert cs.effective_actions(cs.get_connector_type(SERVICE), broken) == []
    assert _run(world, custom, "list_things").json()["error"]["code"] == "CONNECTION_DISABLED_ACTION"


def test_resolve_says_no_connection_allows_the_action_instead_of_calling_it_ambiguous(world):
    from app.security.effective_authority import system_authority

    world["binding"]("Read only", "read_only")
    with pytest.raises(APIError) as caught:
        cs.resolve_authorized_binding(system_authority("test"), connector_type_id=SERVICE, action="create_thing")
    assert caught.value.code == "NO_ALLOWED_CONNECTION" and caught.value.status_code == 403


# ---- does the limit hold against the agents that use the connection? ----------------------------------------------

def _vault(world):
    from app.database import get_db
    from app.services import workspace_service

    with get_db() as conn:
        owner = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
    workspace_service.create_workspace("vault", "vault", owner)


def _bind_with_credential(world, credential_scope, name):
    from app.services import credential_service

    credential = credential_service.create_credential(credential_scope, f"cred-{name}", "test-secret-value", created_by="admin")
    return cs.create_binding(SERVICE, name, "workspace:ops", credential_id=credential["id"],
                             config_json='{"base_url": "http://example.test"}', actions_mode="read_only")


def test_the_limit_holds_when_the_agents_using_the_connection_cannot_fetch_its_credential(world):
    # the credential is in a workspace the agent does not read (an agent reads its own scopes and its person's user scope)
    _vault(world)
    binding = _bind_with_credential(world, "workspace:vault", "Held")
    reach = cs.credential_reach(binding)
    assert reach["agents"] == ["advisor"] and reach["can_fetch"] == [] and reach["credential_scope"] == "workspace:vault"
    sentence = cs.credential_reach_sentence(reach)
    assert sentence["holds"] is True
    assert sentence["text"] == "The agents using this connection cannot fetch its credential, so this limit holds."


def test_an_agent_can_read_its_persons_own_credentials_so_a_limit_there_is_advisory(world):
    binding = _bind_with_credential(world, "user:admin", "Persons own")  # every agent reads its person's user scope
    reach = cs.credential_reach(binding)
    assert reach["can_fetch"] == ["advisor"]
    assert cs.credential_reach_sentence(reach)["holds"] is False


def test_the_limit_is_advisory_when_an_agent_using_the_connection_can_fetch_its_credential(world):
    binding = _bind_with_credential(world, "workspace:ops", "Advisory")  # a scope the advisor reads
    reach = cs.credential_reach(binding)
    assert reach["can_fetch"] == ["advisor"]
    sentence = cs.credential_reach_sentence(reach)
    assert sentence["holds"] is False
    assert sentence["text"] == (
        "This limits what advisor can do through Agent Core. They can still fetch the credential and call the service "
        "directly; store it in a scope they can't read to make the limit hold."
    )
    assert "workspace:" not in sentence["text"]  # no scope syntax in the sentence


def test_a_connection_with_no_credential_or_no_agent_says_the_limit_holds(world):
    nothing = world["binding"]("No credential", "read_only")
    assert cs.credential_reach_sentence(cs.credential_reach(nothing))["text"].startswith("This connection uses no credential")
    from app.services import credential_service

    credential = credential_service.create_credential("workspace:ops", "cred-lonely", "test-secret-value", created_by="admin")
    lonely = cs.create_binding(SERVICE, "Nobody uses it", "workspace:nobody-reads", credential_id=credential["id"],
                               config_json='{"base_url": "http://example.test"}')
    assert cs.credential_reach_sentence(cs.credential_reach(lonely))["text"] == "No agent uses this connection, so this limit holds."


def test_many_agents_are_named_briefly():
    assert cs._names(["a"]) == "a" and cs._names(["a", "b"]) == "a and b" and cs._names(["a", "b", "c"]) == "a, b and c"
    assert cs._names(["a", "b", "c", "d", "e"]) == "a, b, c and 2 more"


def test_the_dialog_asks_the_server_per_connection_and_only_a_person_may(world):
    _vault(world)
    held = _bind_with_credential(world, "workspace:vault", "Held")
    client = world["client"]
    ok = client.get(f"/api/connector-bindings/{held['id']}/credential-reach", headers=_bearer(world["admin"]))
    assert ok.status_code == 200, ok.text
    data = ok.json()["data"]
    assert data["holds"] is True and data["agents"] == ["advisor"] and data["credential_scope"] == "workspace:vault"
    # an agent is not told how its own limit could be got round
    assert client.get(f"/api/connector-bindings/{held['id']}/credential-reach", headers=_bearer(world["key"])).status_code == 403
    page_js = client.get("/static/js/connectors.js").text
    assert "credential-reach" in page_js and "loadCredentialReach" in page_js


# ---- the summary for a plain web service (its actions are the requests its connection allows) -------------------

def _plain_web_binding(world, allowed, **kwargs):
    cs.create_connector_type(
        connector_type_id="plain-web", display_name="Plain web", auth_type="none", backend_type="generic_http",
        endpoint_url="https://web.example.test", supported_actions=[],
    )
    config = json.dumps({"allowed": allowed} if allowed is not None else {})
    return cs.create_binding("plain-web", "Plain web connection", "workspace:ops", config_json=config, **kwargs)


def _summary_row(world, binding):
    from app.security.effective_authority import EffectiveAuthority
    from app.security.scope_enforcer import build_agent_context
    from app.services import agent_service

    context = build_agent_context(agent_service.get_agent_by_id("advisor"))
    summary = cs.build_capability_summary(EffectiveAuthority(context=context).connector_enforcer(), enabled_only=False)
    ct = next(c for c in summary["connectors"] if c["id"] == "plain-web")
    return ct, next(b for b in ct["bindings"] if b["id"] == binding["id"])


def test_a_plain_web_service_counts_its_connections_allowed_requests(world):
    binding = _plain_web_binding(world, ["GET /a", "GET /b", "POST /c"])
    ct, row = _summary_row(world, binding)
    assert row["usable_by_caller"] is True and "not_usable_because" not in row
    assert row["actions"] == {"mode": "all", "allowed": 3, "total": 3} and ct["action_count"] == 3


def test_a_read_only_plain_web_connection_shows_n_of_m_and_none_allowed_says_why(world):
    binding = _plain_web_binding(world, ["GET /a", "GET /b", "POST /c"], actions_mode="read_only")
    _, row = _summary_row(world, binding)
    assert row["usable_by_caller"] is True and row["actions"] == {"mode": "read_only", "allowed": 2, "total": 3}
    only_writes = cs.update_binding(binding["id"], config_json=json.dumps({"allowed": ["POST /c"]}))
    _, row = _summary_row(world, cs.get_binding(binding["id"]))
    assert row["usable_by_caller"] is False and row["actions"]["allowed"] == 0
    assert row["not_usable_because"].startswith("This connection allows none of the service's actions")


def test_not_usable_always_says_why(world):
    nothing_listed = _plain_web_binding(world, None)
    _, row = _summary_row(world, nothing_listed)
    assert row["usable_by_caller"] is False
    assert row["not_usable_because"] == "The service lists no actions and this connection lists no allowed requests."
    cs.update_binding(nothing_listed["id"], config_json=json.dumps({"allowed": ["GET /a"]}), enabled=False)
    summary_off = _summary_row(world, cs.get_binding(nothing_listed["id"]))[1]
    assert summary_off["not_usable_because"] == "This connection is switched off."
