from app.security.scope_enforcer import build_agent_context
from app.services import agent_service, auth_service, connector_service, credential_service


def _mcp(client, token, tool, params):
    return client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token}"},
        json={"tool": tool, "params": params},
    )


def _create_principal_agent(user_id: str, agent_id: str, inherit_user_bindings: bool = True):
    auth_service.create_user(
        user_id,
        f"{user_id}@test.local",
        "testpassword123",
        user_id.title(),
        "user",
    )
    return agent_service.create_agent(
        agent_id,
        f"{user_id.title()} agent",
        user_id,
        default_user_id=user_id,
        read_scopes=[],
        write_scopes=[],
        inherit_user_bindings=inherit_user_bindings,
    )


def _create_user_binding(user_id: str):
    connector_service.create_connector_type(
        connector_type_id="lifequery",
        display_name="LifeQuery",
        auth_type="none",
        supported_actions=[
            {"name": "GET /spaces", "method": "GET"},
            {"name": "POST /spaces", "method": "POST"},
        ],
        backend_type="generic_http",
    )
    credential = credential_service.create_credential(
        f"user:{user_id}", "lifequery", "test-secret", created_by=user_id
    )
    binding = connector_service.create_binding(
        "lifequery",
        "LifeQuery",
        f"user:{user_id}",
        credential_id=credential["id"],
        logical_alias="primary",
        created_by=user_id,
    )
    return credential, binding


def test_active_principal_user_scope_is_inherited_for_binding_discovery_and_resolution(
    test_client,
):
    agent, token = _create_principal_agent("owner", "assistant")
    _, binding = _create_user_binding("owner")

    context = build_agent_context(agent)
    assert "user:owner" in context.read_scopes
    assert "user:owner" not in context.write_scopes

    authority = _mcp(test_client, token, "effective_authority", {})
    assert authority.status_code == 200, authority.json()
    effective = authority.json()["data"]["authority"]
    assert "user:owner" in effective["effective_read_scopes"]
    assert "user:owner" not in effective["effective_write_scopes"]

    listed = _mcp(
        test_client, token, "connectors_bindings_list", {"scope": "user:owner"}
    )
    assert listed.status_code == 200, listed.json()
    assert [item["id"] for item in listed.json()["data"]["bindings"]] == [binding["id"]]

    resolved = _mcp(
        test_client,
        token,
        "connectors_resolve",
        {"connector_type_id": "lifequery", "logical_alias": "primary"},
    )
    assert resolved.status_code == 200, resolved.json()
    assert resolved.json()["data"]["binding"]["id"] == binding["id"]


def test_principal_user_scope_is_isolated_and_removed_when_principal_is_disabled(
    test_client,
):
    _, owner_token = _create_principal_agent("owner", "assistant")
    _, alice_token = _create_principal_agent("alice", "alice-agent")
    _, binding = _create_user_binding("owner")

    alice_list = _mcp(test_client, alice_token, "connectors_bindings_list", {})
    assert alice_list.status_code == 200, alice_list.json()
    assert binding["id"] not in {
        item["id"] for item in alice_list.json()["data"]["bindings"]
    }
    alice_scoped = _mcp(
        test_client, alice_token, "connectors_bindings_list", {"scope": "user:owner"}
    )
    assert alice_scoped.status_code == 403, alice_scoped.json()
    alice_resolve = _mcp(
        test_client,
        alice_token,
        "connectors_resolve",
        {"connector_type_id": "lifequery", "logical_alias": "primary"},
    )
    assert alice_resolve.json()["ok"] is False
    assert alice_resolve.json().get("data", {}).get("binding") is None

    updated, message = auth_service.update_user("owner", is_active=False)
    assert updated, message
    context = build_agent_context(agent_service.get_agent_by_id("assistant"))
    assert "user:owner" not in context.read_scopes
    disabled_list = _mcp(test_client, owner_token, "connectors_bindings_list", {})
    assert disabled_list.status_code == 200, disabled_list.json()
    assert binding["id"] not in {
        item["id"] for item in disabled_list.json()["data"]["bindings"]
    }
    disabled_resolve = _mcp(
        test_client,
        owner_token,
        "connectors_resolve",
        {"connector_type_id": "lifequery", "logical_alias": "primary"},
    )
    assert disabled_resolve.json()["ok"] is False
    assert disabled_resolve.json().get("data", {}).get("binding") is None


def test_inherited_user_scope_allows_all_binding_actions_but_not_secret_reveal(
    test_client, monkeypatch
):
    _, token = _create_principal_agent("owner", "assistant")
    credential, binding = _create_user_binding("owner")
    monkeypatch.setattr(
        connector_service,
        "execute_authorized_binding_action_with_logging",
        lambda binding_id, action, params, authority: {
            "success": True,
            "action": action,
        },
    )

    read_action = _mcp(
        test_client,
        token,
        "connectors_run",
        {"binding_id": binding["id"], "action": "GET /spaces", "params": {}},
    )
    assert read_action.status_code == 200, read_action.json()
    assert read_action.json()["ok"] is True

    write_action = _mcp(
        test_client,
        token,
        "connectors_run",
        {"binding_id": binding["id"], "action": "POST /spaces", "params": {}},
    )
    assert write_action.status_code == 200, write_action.json()
    assert write_action.json()["ok"] is True
    assert write_action.json()["data"]["action"] == "POST /spaces"

    reveal = test_client.post(
        f"/api/credentials/entries/{credential['id']}/reveal",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert reveal.status_code == 403, reveal.json()
    assert reveal.json()["error"]["code"] == "FORBIDDEN"


def test_a_new_agent_does_not_run_its_persons_own_services_until_told_to(test_client, monkeypatch):
    agent, token = _create_principal_agent("owner", "assistant", inherit_user_bindings=False)
    _, binding = _create_user_binding("owner")
    ran = []
    monkeypatch.setattr(connector_service, "execute_binding_action",
                        lambda binding_id, action, params=None: ran.append(action) or {"success": True})

    # It still reads the person's memory scope...
    context = build_agent_context(agent)
    assert "user:owner" in context.read_scopes
    assert "user:owner" not in context.connector_read_scopes()
    # ...but sees none of the services bound there, cannot resolve them, and cannot run them.
    assert _mcp(test_client, token, "connectors_bindings_list", {"scope": "user:owner"}).status_code == 403
    listed = _mcp(test_client, token, "connectors_bindings_list", {})
    assert listed.json()["data"]["bindings"] == []
    resolved = _mcp(test_client, token, "connectors_resolve", {"connector_type_id": "lifequery", "logical_alias": "primary"})
    assert resolved.json().get("data", {}).get("binding") is None
    run = _mcp(test_client, token, "connectors_run", {"binding_id": binding["id"], "action": "GET /spaces", "params": {}})
    assert run.json()["ok"] is False and ran == []
    tested = _mcp(test_client, token, "connectors_bindings_test", {"binding_id": binding["id"]})
    assert tested.status_code == 403
    summary = _mcp(test_client, token, "connectors_summary", {}).json()["data"]
    assert all(not c["bindings"] for c in summary["connectors"])

    # Switching it on is the one thing that gives it back.
    agent_service.update_agent("assistant", inherit_user_bindings=True)
    again = _mcp(test_client, token, "connectors_run", {"binding_id": binding["id"], "action": "GET /spaces", "params": {}})
    assert again.json()["ok"] is True and ran == ["GET /spaces"]


def test_services_in_a_workspace_the_agent_is_given_are_not_affected(test_client):
    from app.services import workspace_service

    _create_principal_agent("owner", "assistant", inherit_user_bindings=False)
    workspace_service.create_workspace("shop", "Shop", "owner")
    connector_service.create_connector_type(
        connector_type_id="shopsvc", display_name="Shop", auth_type="none",
        supported_actions=[{"name": "GET /x", "method": "GET"}], backend_type="generic_http")
    binding = connector_service.create_binding("shopsvc", "Shop", "workspace:shop", created_by="owner")
    agent_service.update_agent("assistant", read_scopes=["workspace:shop"], write_scopes=["workspace:shop"])
    agent = agent_service.get_agent_by_id("assistant")
    from app.security.effective_authority import permanent_authority

    authority = permanent_authority(build_agent_context(agent))
    assert authority.can("connector", "read", scope="workspace:shop")
    assert not authority.can("connector", "read", scope="user:owner")
    assert binding["scope"] == "workspace:shop"


def test_upgrade_keeps_what_agents_could_do_and_says_which_ones(test_client):

    from app.database import get_db
    from app.schema import _migrate_021_inherit_user_bindings

    auth_service.create_user("person", "person@test.local", "testpassword123", "Person", "user")
    auth_service.create_user("gone", "gone@test.local", "testpassword123", "Gone", "user")
    for agent_id, user, caps in (("runner", "person", ["memory", "connectors_execute"]),
                                 ("reader", "person", ["memory", "connectors_read"]),
                                 ("orphan", "gone", ["connectors_execute"])):
        agent_service.create_agent(agent_id, agent_id, user, default_user_id=user, capabilities=caps)
    with get_db() as conn:
        conn.execute("UPDATE users SET is_active = 0 WHERE id = 'gone'")
        conn.execute("ALTER TABLE agents DROP COLUMN inherit_user_bindings")
        conn.commit()
        _migrate_021_inherit_user_bindings(conn)
        conn.commit()
        flags = {r["id"]: r["inherit_user_bindings"] for r in conn.execute("SELECT id, inherit_user_bindings FROM agents")}
        audited = [r["resource_id"] for r in conn.execute(
            "SELECT resource_id, details_json FROM audit_log WHERE actor_id = 'upgrade' AND action = 'agent_updated'")]
    assert flags["runner"] == 1 and flags["reader"] == 0 and flags["orphan"] == 0
    assert audited == ["runner"]
    # New agents start with it off.
    agent_service.create_agent("fresh", "Fresh", "person", capabilities=["connectors_execute"])
    assert agent_service.get_agent_by_id("fresh")["inherit_user_bindings"] == 0


def test_only_an_admin_or_the_person_themselves_can_let_an_agent_run_that_persons_services(test_client):
    from tests.conftest import csrf_headers

    auth_service.create_user("boss", "boss@test.local", "testpassword123", "Boss", "user")
    auth_service.create_user("helper", "helper@test.local", "testpassword123", "Helper", "user")
    agent_service.create_agent("bosses-agent", "Boss Agent", "helper", default_user_id="boss", capabilities=["connectors_execute"])
    agent_service.create_agent("own-agent", "Own Agent", "helper", default_user_id="helper", capabilities=["connectors_execute"])
    test_client.cookies.clear()
    test_client.cookies.set("session_token", auth_service.create_session("helper")["session_id"])
    # The helper manages both agents but acts for the boss on one of them.
    refused = test_client.put("/api/agents/bosses-agent", headers=csrf_headers(test_client), json={"inherit_user_bindings": True})
    assert refused.status_code == 403
    assert agent_service.get_agent_by_id("bosses-agent")["inherit_user_bindings"] == 0
    allowed = test_client.put("/api/agents/own-agent", headers=csrf_headers(test_client), json={"inherit_user_bindings": True})
    assert allowed.status_code == 200, allowed.text
    assert agent_service.get_agent_by_id("own-agent")["inherit_user_bindings"] == 1


def test_testing_a_binding_needs_the_same_capability_as_running_one(test_client, monkeypatch):
    auth_service.create_user("boss2", "boss2@test.local", "testpassword123", "Boss Two", "user")
    agent_service.create_agent("looker", "Looker", "boss2", capabilities=["connectors_read"], read_scopes=["workspace:ops"], write_scopes=[])
    agent_service.create_agent("doer", "Doer", "boss2", capabilities=["connectors_read", "connectors_execute"], read_scopes=["workspace:ops"], write_scopes=[])
    from app.services import workspace_service

    workspace_service.create_workspace("ops", "Ops", "boss2")
    connector_service.create_connector_type(connector_type_id="tsvc", display_name="T", auth_type="none",
                                            supported_actions=[{"name": "GET /x", "method": "GET"}], backend_type="generic_http")
    binding = connector_service.create_binding("tsvc", "T", "workspace:ops", created_by="boss2")
    monkeypatch.setattr(connector_service, "test_binding", lambda binding_id: {"success": True})
    keys = {}
    for agent_id in ("looker", "doer"):
        keys[agent_id] = agent_service.rotate_agent_key(agent_id)
    refused = _mcp(test_client, keys["looker"], "connectors_bindings_test", {"binding_id": binding["id"]})
    assert refused.status_code == 403
    rest = test_client.post(f"/api/connector-bindings/{binding['id']}/test", headers={"Authorization": f"Bearer {keys['looker']}"})
    assert rest.status_code == 403 and rest.json()["error"]["code"] == "CAPABILITY_DENIED"
    assert _mcp(test_client, keys["doer"], "connectors_bindings_test", {"binding_id": binding["id"]}).json()["ok"] is True
    # Looking is still allowed with read alone.
    assert _mcp(test_client, keys["looker"], "connectors_bindings_list", {}).status_code == 200
