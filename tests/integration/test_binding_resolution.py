from app.services import connector_service


def _type_and_bindings():
    connector_service.create_connector_type(
        connector_type_id="calendar",
        display_name="Calendar",
        auth_type="none",
        supported_actions=[{"name": "create_event", "side_effect": "write"}],
        provider_type="openapi",
        backend_type="generic_http",
    )
    first = connector_service.create_binding(
        "calendar", "Personal", "user:admin", logical_alias="primary", priority=0
    )
    second = connector_service.create_binding(
        "calendar", "Workspace", "workspace:home", logical_alias="primary", priority=0
    )
    return first, second


def test_resolution_never_guesses_across_scopes(test_client, admin_token):
    first, second = _type_and_bindings()
    ambiguous = test_client.post(
        "/api/connector-bindings/resolve",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"connector_type_id": "calendar", "logical_alias": "primary"},
    )
    assert ambiguous.status_code == 409
    assert ambiguous.json()["error"]["code"] == "AMBIGUOUS_BINDING"

    resolved = test_client.post(
        "/api/connector-bindings/resolve",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "connector_type_id": "calendar", "logical_alias": "primary",
            "scope": "user:admin", "action": "create_event",
        },
    )
    assert resolved.status_code == 200, resolved.json()
    assert resolved.json()["data"]["binding"]["id"] == first["id"]
    assert resolved.json()["data"]["binding"]["id"] != second["id"]


def test_preferred_then_unique_priority_and_mcp_parity(test_client, admin_token):
    first, second = _type_and_bindings()
    connector_service.update_binding(first["id"], priority=5)
    rest = test_client.post(
        "/api/connector-bindings/resolve",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"connector_type_id": "calendar"},
    )
    assert rest.status_code == 200
    assert rest.json()["data"]["binding"]["id"] == first["id"]
    mcp = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"tool": "connectors_resolve", "params": {"connector_type_id": "calendar"}},
    )
    assert mcp.status_code == 200, mcp.json()
    assert mcp.json()["data"]["binding"]["id"] == first["id"]

    connector_service.update_binding(second["id"], is_preferred=True)
    preferred = test_client.post(
        "/api/connector-bindings/resolve",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"connector_type_id": "calendar"},
    )
    assert preferred.json()["data"]["binding"]["id"] == second["id"]


def test_delegated_resolution_filters_exact_binding_and_action(
    test_client, admin_token, agent_token
):
    first, second = _type_and_bindings()
    grant = test_client.post(
        "/api/delegations",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "recipient_agent_id": "testagent", "purpose": "calendar", "ttl_seconds": 60,
            "binding_actions": [{"binding_id": first["id"], "action": "create_event"}],
        },
    )
    assert grant.status_code == 201, grant.json()
    grant_id = grant.json()["data"]["grant"]["id"]
    secret = test_client.post(
        f"/api/delegations/{grant_id}/claim",
        headers={"Authorization": f"Bearer {agent_token}"},
    ).json()["data"]["grant_secret"]
    resolved = test_client.post(
        "/api/connector-bindings/resolve",
        headers={"Authorization": f"Bearer {agent_token}", "X-Agent-Core-Grant": secret},
        json={"connector_type_id": "calendar", "action": "create_event"},
    )
    assert resolved.status_code == 200, resolved.json()
    assert resolved.json()["data"]["binding"]["id"] == first["id"]
    assert resolved.json()["data"]["binding"]["id"] != second["id"]
