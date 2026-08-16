def _request(test_client, agent_token):
    response = test_client.post(
        "/api/delegation-requests",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "recipient_agent_id": "testagent",
            "purpose": "read approved context",
            "ttl_seconds": 300,
            "scope_permissions": [
                {"resource_type": "memory", "operation": "read", "scope": "user:admin"},
                {"resource_type": "briefing", "operation": "read", "scope": "user:admin"},
            ],
            "correlation_id": "corr-1",
        },
    )
    assert response.status_code == 201, response.json()
    return response.json()["data"]["request"]


def test_delegation_requests_dashboard_supports_human_review(
    test_client, admin_token, agent_token
):
    request = _request(test_client, agent_token)
    test_client.cookies.set("session_token", admin_token)

    page = test_client.get("/delegation-requests")
    assert page.status_code == 200, page.text
    assert "Delegation Requests" in page.text
    assert request["id"] in page.text
    assert "read approved context" in page.text
    assert "data-delegation-approve" in page.text
    assert "data-delegation-deny" in page.text
    assert "Approval can only keep or narrow" in page.text
    assert "grant_secret" not in page.text
    assert "/api/delegation-requests/" in page.text


def test_requester_cannot_self_approve_but_admin_can_narrow(
    test_client, admin_token, agent_token
):
    request = _request(test_client, agent_token)
    assert request["requester_actor_id"] == "testagent"
    assert request["coordinator_agent_id"] == "testagent"
    assert request["target_user_id"] == "admin"

    self_approve = test_client.post(
        f"/api/delegation-requests/{request['id']}/approve",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={},
    )
    assert self_approve.status_code == 403

    approved = test_client.post(
        f"/api/delegation-requests/{request['id']}/approve",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "scope_permissions": [
                {"resource_type": "memory", "operation": "read", "scope": "user:admin"}
            ]
        },
    )
    assert approved.status_code == 200, approved.json()
    data = approved.json()["data"]
    assert data["request"]["status"] == "approved"
    assert data["grant"]["status"] == "approved_unclaimed"
    assert data["grant"]["coordinator_agent_id"] == "testagent"
    assert "secret_hash" not in data["grant"]


def test_approval_cannot_expand_and_decisions_are_one_time(
    test_client, admin_token, agent_token
):
    request = _request(test_client, agent_token)
    expanded = test_client.post(
        f"/api/delegation-requests/{request['id']}/approve",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "scope_permissions": [
                {"resource_type": "memory", "operation": "write", "scope": "user:admin"}
            ]
        },
    )
    assert expanded.status_code == 400
    assert expanded.json()["error"]["code"] == "APPROVAL_EXPANDS_REQUEST"

    denied = test_client.post(
        f"/api/delegation-requests/{request['id']}/deny",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"reason": "not needed"},
    )
    assert denied.status_code == 200
    repeat = test_client.post(
        f"/api/delegation-requests/{request['id']}/deny",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={},
    )
    assert repeat.status_code == 409


def test_request_shape_is_closed_and_delegated_credentials_cannot_request(
    test_client, admin_token, agent_token
):
    invalid = test_client.post(
        "/api/delegation-requests",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "recipient_agent_id": "testagent", "purpose": "bad", "ttl_seconds": 60,
            "scope_permissions": [
                {"resource_type": "credential", "operation": "read", "scope": "user:admin"}
            ],
        },
    )
    assert invalid.status_code == 400

    direct = test_client.post(
        "/api/delegations",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "recipient_agent_id": "testagent", "purpose": "read", "ttl_seconds": 60,
            "scope_permissions": [
                {"resource_type": "memory", "operation": "read", "scope": "user:admin"}
            ],
        },
    ).json()["data"]["grant"]
    secret = test_client.post(
        f"/api/delegations/{direct['id']}/claim",
        headers={"Authorization": f"Bearer {agent_token}"},
    ).json()["data"]["grant_secret"]
    delegated = test_client.post(
        "/api/delegation-requests",
        headers={"Authorization": f"Bearer {agent_token}", "X-Agent-Core-Grant": secret},
        json={
            "recipient_agent_id": "testagent", "purpose": "nested", "ttl_seconds": 60,
            "scope_permissions": [
                {"resource_type": "memory", "operation": "read", "scope": "user:admin"}
            ],
        },
    )
    assert delegated.status_code == 403


def test_rest_and_mcp_share_request_and_approval_decisions(test_client, admin_token, agent_token):
    requested = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "tool": "delegation_request",
            "params": {
                "recipient_agent_id": "testagent", "purpose": "parity", "ttl_seconds": 60,
                "scope_permissions": [
                    {"resource_type": "memory", "operation": "read", "scope": "user:admin"}
                ],
            },
        },
    )
    assert requested.status_code == 201, requested.json()
    request_id = requested.json()["data"]["request"]["id"]
    approved = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"tool": "delegation_request_approve", "params": {"request_id": request_id}},
    )
    assert approved.status_code == 200, approved.json()
    assert approved.json()["data"]["request"]["status"] == "approved"
