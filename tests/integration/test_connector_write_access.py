"""A visible binding grants every action enabled by its connector type."""

from app.services import agent_service, connector_service


def _binding():
    connector_service.create_connector_type(
        connector_type_id="action-access",
        display_name="Action Access",
        auth_type="none",
        supported_actions=[
            {"name": "GET /items", "method": "GET"},
            {"name": "DELETE /items", "method": "DELETE"},
        ],
        backend_type="generic_http",
    )
    return connector_service.create_binding(
        "action-access", "Binding", "workspace:proj"
    )


def _binding_agent(client, admin_token):
    created = client.post(
        "/api/workspaces",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"id": "proj", "name": "Proj"},
    )
    assert created.status_code == 201, created.json()
    _, token = agent_service.create_agent(
        "binding-agent",
        "Binding agent",
        "admin",
        read_scopes=["workspace:proj"],
        write_scopes=[],
    )
    return token


def _successful_execution(binding_id, action, params, authority):
    return {"success": True, "action": action}


def test_binding_visibility_allows_every_enabled_action_over_mcp(
    test_client, admin_token, monkeypatch
):
    binding = _binding()
    token = _binding_agent(test_client, admin_token)
    monkeypatch.setattr(
        connector_service,
        "execute_authorized_binding_action_with_logging",
        _successful_execution,
    )

    response = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "tool": "connectors_run",
            "params": {
                "binding_id": binding["id"],
                "action": "DELETE /items",
                "params": {},
            },
        },
    )
    assert response.status_code == 200, response.json()
    assert response.json()["ok"] is True
    assert response.json()["data"]["action"] == "DELETE /items"


def test_binding_visibility_allows_every_enabled_action_over_rest(
    test_client, admin_token, monkeypatch
):
    binding = _binding()
    token = _binding_agent(test_client, admin_token)
    monkeypatch.setattr(
        connector_service,
        "execute_authorized_binding_action_with_logging",
        _successful_execution,
    )

    response = test_client.post(
        f"/api/connector-bindings/{binding['id']}/run",
        headers={"Authorization": f"Bearer {token}"},
        json={"action": "DELETE /items", "params": {}},
    )
    assert response.status_code == 200, response.json()
    assert response.json()["data"]["result"]["action"] == "DELETE /items"
