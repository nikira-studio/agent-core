from app.security.scope_enforcer import build_agent_context
from app.services import agent_service, auth_service, connector_service, credential_service


def _mcp(client, token, tool, params):
    return client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token}"},
        json={"tool": tool, "params": params},
    )


def _create_principal_agent(user_id: str, agent_id: str):
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
    agent, token = _create_principal_agent("brian", "sage")
    _, binding = _create_user_binding("brian")

    context = build_agent_context(agent)
    assert "user:brian" in context.read_scopes
    assert "user:brian" not in context.write_scopes

    authority = _mcp(test_client, token, "effective_authority", {})
    assert authority.status_code == 200, authority.json()
    effective = authority.json()["data"]["authority"]
    assert "user:brian" in effective["effective_read_scopes"]
    assert "user:brian" not in effective["effective_write_scopes"]

    listed = _mcp(
        test_client, token, "connectors_bindings_list", {"scope": "user:brian"}
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
    _, brian_token = _create_principal_agent("brian", "sage")
    _, alice_token = _create_principal_agent("alice", "alice-agent")
    _, binding = _create_user_binding("brian")

    alice_list = _mcp(test_client, alice_token, "connectors_bindings_list", {})
    assert alice_list.status_code == 200, alice_list.json()
    assert binding["id"] not in {
        item["id"] for item in alice_list.json()["data"]["bindings"]
    }
    alice_scoped = _mcp(
        test_client, alice_token, "connectors_bindings_list", {"scope": "user:brian"}
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

    updated, message = auth_service.update_user("brian", is_active=False)
    assert updated, message
    context = build_agent_context(agent_service.get_agent_by_id("sage"))
    assert "user:brian" not in context.read_scopes
    disabled_list = _mcp(test_client, brian_token, "connectors_bindings_list", {})
    assert disabled_list.status_code == 200, disabled_list.json()
    assert binding["id"] not in {
        item["id"] for item in disabled_list.json()["data"]["bindings"]
    }
    disabled_resolve = _mcp(
        test_client,
        brian_token,
        "connectors_resolve",
        {"connector_type_id": "lifequery", "logical_alias": "primary"},
    )
    assert disabled_resolve.json()["ok"] is False
    assert disabled_resolve.json().get("data", {}).get("binding") is None


def test_inherited_user_scope_allows_all_binding_actions_but_not_secret_reveal(
    test_client, monkeypatch
):
    _, token = _create_principal_agent("brian", "sage")
    credential, binding = _create_user_binding("brian")
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
