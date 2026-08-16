import json
import re


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
    match = re.search(r"const delegationRequests = (.+);", page.text)
    assert match is not None
    embedded_requests = json.loads(match.group(1))
    assert request["id"] in embedded_requests


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


def test_request_lifecycle_notifies_stream_and_webhooks(
    test_client, admin_token, agent_token, monkeypatch
):
    from app.services import webhook_service
    from app.services.event_stream_service import event_hub

    assert "delegation_request_created" in webhook_service.WEBHOOK_EVENT_TYPES
    assert "delegation_request_approved" in webhook_service.WEBHOOK_EVENT_TYPES
    assert "delegation_request_denied" in webhook_service.WEBHOOK_EVENT_TYPES

    seen = []
    monkeypatch.setattr(
        event_hub, "publish", lambda event, data: seen.append(("stream", event, data))
    )
    monkeypatch.setattr(
        webhook_service,
        "dispatch_event",
        lambda event, data: seen.append(("webhook", event, data)),
    )

    request = _request(test_client, agent_token)
    created = [item for item in seen if item[1] == "delegation_request_created"]
    assert {kind for kind, _, _ in created} == {"stream", "webhook"}
    payload = created[0][2]
    assert payload["request_id"] == request["id"]
    assert payload["status"] == "pending"
    assert payload["scope_permission_count"] == 2
    assert "scope_permissions" not in payload

    test_client.cookies.set("session_token", admin_token)
    overview = test_client.get("/")
    assert overview.status_code == 200
    assert "Pending Delegations" in overview.text
    assert "/delegation-requests" in overview.text

    seen.clear()
    denied = test_client.post(
        f"/api/delegation-requests/{request['id']}/deny",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"reason": "not needed"},
    )
    assert denied.status_code == 200
    assert {(kind, event) for kind, event, _ in seen} == {
        ("stream", "delegation_request_denied"),
        ("webhook", "delegation_request_denied"),
    }
    assert seen[0][2]["decision_reason"] == "not needed"

    seen.clear()
    second = _request(test_client, agent_token)
    approved = test_client.post(
        f"/api/delegation-requests/{second['id']}/approve",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={},
    )
    assert approved.status_code == 200
    events = {event for _, event, _ in seen}
    assert "delegation_request_approved" in events
    approved_payload = next(d for _, e, d in seen if e == "delegation_request_approved")
    assert approved_payload["grant_id"]
    assert "grant_secret" not in approved_payload


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
