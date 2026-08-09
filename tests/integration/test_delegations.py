from app.database import get_db


def _issue(test_client, admin_token):
    response = test_client.post(
        "/api/delegations",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "recipient_agent_id": "testagent",
            "purpose": "Read task context",
            "ttl_seconds": 300,
            "scope_permissions": [
                {"resource_type": "memory", "operation": "read", "scope": "user:admin"}
            ],
        },
    )
    assert response.status_code == 201, response.json()
    return response.json()["data"]["grant"]


def test_recipient_claims_once_and_header_replaces_permanent_authority(
    test_client, admin_token, agent_token
):
    grant = _issue(test_client, admin_token)
    assert "secret_hash" not in grant
    assert "grant_secret" not in grant

    claim = test_client.post(
        f"/api/delegations/{grant['id']}/claim",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert claim.status_code == 200, claim.json()
    secret = claim.json()["data"]["grant_secret"]
    assert secret.startswith(f"ac_dg_{grant['id']}.")

    second = test_client.post(
        f"/api/delegations/{grant['id']}/claim",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert second.status_code == 409

    inspected = test_client.get(
        "/api/auth/effective-authority",
        headers={
            "Authorization": f"Bearer {agent_token}",
            "X-Agent-Core-Grant": secret,
        },
    )
    assert inspected.status_code == 200, inspected.json()
    authority = inspected.json()["data"]["authority"]
    assert authority["authorization_mode"] == "delegated"
    assert authority["grant_id"] == grant["id"]
    assert authority["principal_user_id"] == "admin"
    assert authority["resource_permissions"] == [
        {"resource_type": "memory", "operation": "read", "scope": "user:admin"}
    ]

    with get_db() as conn:
        row = conn.execute(
            "SELECT secret_hash FROM delegated_grants WHERE id = ?", (grant["id"],)
        ).fetchone()
        audits = conn.execute(
            "SELECT details_json FROM audit_log WHERE resource_id = ?", (grant["id"],)
        ).fetchall()
    assert row["secret_hash"] and row["secret_hash"] != secret
    assert all(secret not in (item["details_json"] or "") for item in audits)

    written = test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "scope": "user:admin", "memory_class": "fact", "content": "delegated read",
            "topic": "delegation-test", "source_kind": "human_direct",
        },
    )
    assert written.status_code == 201, written.json()
    read = test_client.post(
        "/api/memory/get",
        headers={"Authorization": f"Bearer {agent_token}", "X-Agent-Core-Grant": secret},
        json={"scope": "user:admin"},
    )
    assert read.status_code == 200, read.json()
    assert [row["content"] for row in read.json()["data"]["records"]] == ["delegated read"]
    mcp_read = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}", "X-Agent-Core-Grant": secret},
        json={"tool": "memory_get", "params": {"scope": "user:admin"}},
    )
    assert mcp_read.status_code == 200, mcp_read.json()
    assert [row["content"] for row in mcp_read.json()["data"]["records"]] == ["delegated read"]
    denied_write = test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {agent_token}", "X-Agent-Core-Grant": secret},
        json={
            "scope": "user:admin", "memory_class": "fact", "content": "must fail",
            "topic": "delegation-test", "source_kind": "tool_output",
        },
    )
    assert denied_write.status_code == 403


def test_wrong_recipient_and_revoked_grant_fail(test_client, admin_token, agent_token):
    grant = _issue(test_client, admin_token)
    claim = test_client.post(
        f"/api/delegations/{grant['id']}/claim",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    secret = claim.json()["data"]["grant_secret"]

    revoked = test_client.post(
        f"/api/delegations/{grant['id']}/revoke",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"reason": "task cancelled"},
    )
    assert revoked.status_code == 200, revoked.json()
    denied = test_client.get(
        "/api/auth/effective-authority",
        headers={"Authorization": f"Bearer {agent_token}", "X-Agent-Core-Grant": secret},
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "GRANT_INACTIVE"


def test_unknown_permissions_and_agent_issuers_fail_closed(test_client, admin_token, agent_token):
    bad = test_client.post(
        "/api/delegations",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "recipient_agent_id": "testagent",
            "purpose": "invalid",
            "ttl_seconds": 60,
            "scope_permissions": [
                {"resource_type": "credential", "operation": "read", "scope": "user:admin"}
            ],
        },
    )
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "INVALID_PERMISSION"

    agent_issue = test_client.post(
        "/api/delegations",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "recipient_agent_id": "testagent",
            "purpose": "not enabled",
            "ttl_seconds": 60,
            "scope_permissions": [
                {"resource_type": "memory", "operation": "read", "scope": "user:admin"}
            ],
        },
    )
    assert agent_issue.status_code == 403
    assert agent_issue.json()["error"]["code"] == "DELEGATION_FORBIDDEN"


def test_grant_credentials_in_body_or_mcp_arguments_are_rejected(
    test_client, admin_token, agent_token
):
    body = test_client.post(
        "/api/memory/get",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"scope": "user:admin", "grant_secret": "must-not-be-read"},
    )
    assert body.status_code == 400
    assert body.json()["error"]["code"] == "GRANT_HEADER_REQUIRED"
    mcp = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"tool": "memory_get", "params": {"scope": "user:admin", "grant_credential": "must-not-be-read"}},
    )
    assert mcp.status_code == 400
    assert mcp.json()["error"]["code"] == "GRANT_HEADER_REQUIRED"


def test_connector_execution_records_delegated_attribution(
    test_client, admin_token, agent_token, monkeypatch
):
    from app.services import connector_service

    connector_service.create_connector_type(
        connector_type_id="attributed", display_name="Attributed", auth_type="none",
        provider_type="openapi", backend_type="generic_http",
        supported_actions=[{"name": "read", "side_effect": "none"}],
    )
    binding = connector_service.create_binding("attributed", "Attributed", "user:admin")
    grant = test_client.post(
        "/api/delegations",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "recipient_agent_id": "testagent", "purpose": "attribution", "ttl_seconds": 60,
            "binding_actions": [{"binding_id": binding["id"], "action": "read"}],
            "correlation_id": "corr-attribution",
        },
    ).json()["data"]["grant"]
    secret = test_client.post(
        f"/api/delegations/{grant['id']}/claim",
        headers={"Authorization": f"Bearer {agent_token}"},
    ).json()["data"]["grant_secret"]
    monkeypatch.setattr(
        connector_service, "execute_binding_action",
        lambda *args, **kwargs: {"success": True, "body": {"ok": True}},
    )
    executed = test_client.post(
        f"/api/connector-bindings/{binding['id']}/run",
        headers={"Authorization": f"Bearer {agent_token}", "X-Agent-Core-Grant": secret},
        json={"action": "read", "params": {}},
    )
    assert executed.status_code == 200, executed.json()
    with get_db() as conn:
        row = conn.execute(
            """SELECT actor_type, actor_id, principal_user_id, executor_agent_id,
                      grant_id, correlation_id, authorization_mode
               FROM connector_executions WHERE binding_id = ?""",
            (binding["id"],),
        ).fetchone()
    assert tuple(row) == (
        "agent", "testagent", "admin", "testagent", grant["id"],
        "corr-attribution", "delegated",
    )


def test_connector_service_boundary_requires_authority(clean_db):
    from app.services import connector_service

    try:
        connector_service.execute_authorized_binding_action_with_logging(
            "missing", "read", {}, None
        )
    except TypeError as exc:
        assert "explicit" in str(exc)
    else:
        raise AssertionError("connector service accepted an absent authority")


def test_principal_disable_and_issuer_scope_downgrade_invalidate_immediately(
    test_client, admin_token, agent_token
):
    from app.services import agent_service, auth_service

    grant = _issue(test_client, admin_token)
    secret = test_client.post(
        f"/api/delegations/{grant['id']}/claim",
        headers={"Authorization": f"Bearer {agent_token}"},
    ).json()["data"]["grant_secret"]
    ok, message = auth_service.update_user("admin", is_active=False)
    assert ok, message
    invalid = test_client.get(
        "/api/auth/effective-authority",
        headers={"Authorization": f"Bearer {agent_token}", "X-Agent-Core-Grant": secret},
    )
    assert invalid.status_code == 403
    assert invalid.json()["error"]["code"] == "GRANT_INVALIDATED"

    auth_service.update_user("admin", is_active=True)
    with get_db() as conn:
        conn.execute("UPDATE agents SET can_delegate = 1 WHERE id = 'testagent'")
        conn.commit()
    agent_grant = test_client.post(
        "/api/delegations",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "recipient_agent_id": "testagent", "purpose": "attenuation", "ttl_seconds": 60,
            "scope_permissions": [
                {"resource_type": "memory", "operation": "read", "scope": "user:admin"}
            ],
        },
    )
    assert agent_grant.status_code == 201, agent_grant.json()
    grant_id = agent_grant.json()["data"]["grant"]["id"]
    agent_secret = test_client.post(
        f"/api/delegations/{grant_id}/claim",
        headers={"Authorization": f"Bearer {agent_token}"},
    ).json()["data"]["grant_secret"]
    agent_service.update_agent(
        "testagent", read_scopes=["agent:testagent"], write_scopes=["agent:testagent"]
    )
    downgraded = test_client.get(
        "/api/auth/effective-authority",
        headers={"Authorization": f"Bearer {agent_token}", "X-Agent-Core-Grant": agent_secret},
    )
    assert downgraded.status_code == 403
    assert downgraded.json()["error"]["code"] == "GRANT_INVALIDATED"
