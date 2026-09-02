def test_credential_create_entry(test_client, agent_token):
    r = test_client.post(
        "/api/credentials/entries",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "scope": "agent:testagent",
            "name": "test-api-key",
            "value": "EXAMPLE_LIVE_API_KEY",
            "label": "Production API Key",
        },
    )
    assert r.status_code == 201, f"create failed: {r.json()}"
    data = r.json()["data"]
    from app.branding import CREDENTIAL_PREFIX
    assert data["entry"]["reference_name"].startswith(f"{CREDENTIAL_PREFIX}TEST_API_KEY_")


def test_credential_create_entry_rejects_duplicate_name(test_client, agent_token):
    first = test_client.post(
        "/api/credentials/entries",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "scope": "agent:testagent",
            "name": "duplicate-test",
            "value": "first-secret",
        },
    )
    assert first.status_code == 201, first.json()

    second = test_client.post(
        "/api/credentials/entries",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "scope": "agent:testagent",
            "name": "duplicate-test",
            "value": "second-secret",
        },
    )
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "DUPLICATE_CREDENTIAL"


def test_credential_rejects_empty_name_on_update(test_client, agent_token):
    create_r = test_client.post(
        "/api/credentials/entries",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"scope": "agent:testagent", "name": "update-test", "value": "secret"},
    )
    assert create_r.status_code == 201
    entry_id = create_r.json()["data"]["entry"]["id"]

    update_r = test_client.put(
        f"/api/credentials/entries/{entry_id}",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"name": ""},
    )
    assert update_r.status_code == 400
    assert update_r.json()["error"]["code"] == "INVALID_NAME"


def test_credential_update_writes_audit_event(test_client, agent_token):
    from app.services import audit_service

    create_r = test_client.post(
        "/api/credentials/entries",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"scope": "agent:testagent", "name": "update-audit", "value": "secret"},
    )
    assert create_r.status_code == 201
    entry_id = create_r.json()["data"]["entry"]["id"]

    update_r = test_client.put(
        f"/api/credentials/entries/{entry_id}",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"label": "Updated Label"},
    )
    assert update_r.status_code == 200
    assert audit_service.query_events(action="credential_entry_updated", resource_type="credential")[-1]["resource_id"] == entry_id


def test_credential_scope_move_preserves_secret_and_reports_linked_bindings(
    test_client, admin_token
):
    from app.database import get_db
    from app.services import audit_service, connector_service

    create_r = test_client.post(
        "/api/credentials/entries",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"scope": "user:admin", "name": "move-me", "value": "secret-value"},
    )
    assert create_r.status_code == 201, create_r.json()
    entry_id = create_r.json()["data"]["entry"]["id"]
    reference_name = create_r.json()["data"]["entry"]["reference_name"]
    connector_type = connector_service.list_connector_types()[0]
    connector_service.create_binding(
        connector_type_id=connector_type["id"],
        name="Move test binding",
        scope="user:admin",
        credential_id=entry_id,
        created_by="admin",
    )
    with get_db() as conn:
        encrypted_before = conn.execute(
            "SELECT value_encrypted FROM credentials WHERE id = ?", (entry_id,)
        ).fetchone()["value_encrypted"]

    move_r = test_client.put(
        f"/api/credentials/entries/{entry_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"scope": "workspace:world-monitor"},
    )
    assert move_r.status_code == 200, move_r.json()
    assert move_r.json()["data"]["move"] == {
        "old_scope": "user:admin",
        "new_scope": "workspace:world-monitor",
        "binding_scopes": ["user:admin"],
    }
    with get_db() as conn:
        moved = conn.execute(
            "SELECT id, scope, reference_name, value_encrypted FROM credentials WHERE id = ?",
            (entry_id,),
        ).fetchone()
    assert dict(moved) == {
        "id": entry_id,
        "scope": "workspace:world-monitor",
        "reference_name": reference_name,
        "value_encrypted": encrypted_before,
    }
    event = audit_service.query_events(
        action="credential_entry_moved", resource_type="credential"
    )[-1]
    assert event["resource_id"] == entry_id


def test_credential_scope_move_requires_write_access_to_destination(test_client, clean_db):
    from app.services.auth_service import create_session, create_user

    create_user("regular", "regular@test.local", "testpassword123", "Regular", "user")
    create_user("other", "other@test.local", "testpassword123", "Other", "user")
    token = create_session("regular")["session_id"]
    create_r = test_client.post(
        "/api/credentials/entries",
        headers={"Authorization": f"Bearer {token}"},
        json={"scope": "user:regular", "name": "private", "value": "secret"},
    )
    entry_id = create_r.json()["data"]["entry"]["id"]

    move_r = test_client.put(
        f"/api/credentials/entries/{entry_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"scope": "user:other"},
    )
    assert move_r.status_code == 403
    assert move_r.json()["error"]["code"] == "SCOPE_DENIED"


def test_credential_scope_move_and_rename_are_atomic(test_client, admin_token):
    first = test_client.post(
        "/api/credentials/entries",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"scope": "user:admin", "name": "source", "value": "one"},
    ).json()["data"]["entry"]
    test_client.post(
        "/api/credentials/entries",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"scope": "workspace:world-monitor", "name": "taken", "value": "two"},
    )

    response = test_client.put(
        f"/api/credentials/entries/{first['id']}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"scope": "workspace:world-monitor", "name": "taken"},
    )
    assert response.status_code == 409

    from app.services import credential_service

    unchanged = credential_service.get_credential(first["id"])
    assert unchanged["scope"] == "user:admin"
    assert unchanged["name"] == "source"


def test_credential_list_entries(test_client, agent_token):
    test_client.post(
        "/api/credentials/entries",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"scope": "agent:testagent", "name": "list-test", "value": "secret"},
    )
    r = test_client.get(
        "/api/credentials/entries?scope=agent:testagent",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert r.status_code == 200
    assert len(r.json()["data"]["entries"]) >= 1


def test_credential_reveal_for_agent_session_is_blocked(test_client, agent_token):
    create_r = test_client.post(
        "/api/credentials/entries",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "scope": "agent:testagent",
            "name": "reveal-test",
            "value": "secret-value",
        },
    )
    assert create_r.status_code == 201
    entry_id = create_r.json()["data"]["entry"]["id"]
    r = test_client.post(
        f"/api/credentials/entries/{entry_id}/reveal",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert r.status_code == 403


def test_credential_reference(test_client, agent_token):
    create_r = test_client.post(
        "/api/credentials/entries",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "scope": "agent:testagent",
            "name": "ref-test",
            "value": "credential-data",
        },
    )
    assert create_r.status_code == 201
    entry_id = create_r.json()["data"]["entry"]["id"]
    r = test_client.post(
        f"/api/credentials/entries/{entry_id}/reference",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert r.status_code == 200
    assert "reference_name" in r.json()["data"]


def test_credential_scopes(test_client, agent_token):
    r = test_client.get(
        "/api/credentials/scopes",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert r.status_code == 200


def test_non_admin_user_can_reveal_own_credential_entry(test_client, clean_db):
    from app.services.auth_service import create_user, create_session

    create_user(
        user_id="regular",
        email="regular@test.local",
        password="testpassword123",
        display_name="Regular User",
        role="user",
    )
    session = create_session("regular")
    token = session["session_id"]

    create_r = test_client.post(
        "/api/credentials/entries",
        headers={"Authorization": f"Bearer {token}"},
        json={"scope": "user:regular", "name": "own-secret", "value": "user-secret"},
    )
    assert create_r.status_code == 201, create_r.json()
    entry_id = create_r.json()["data"]["entry"]["id"]

    reveal_r = test_client.post(
        f"/api/credentials/entries/{entry_id}/reveal",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert reveal_r.status_code == 200, reveal_r.json()
    assert reveal_r.json()["data"]["value"] == "user-secret"


def test_non_admin_user_cannot_reveal_other_user_credential_entry(
    test_client, clean_db
):
    from app.services.auth_service import create_user, create_session
    from app.services.credential_service import create_credential

    create_user(
        "regular", "regular@test.local", "testpassword123", "Regular User", "user"
    )
    create_user("other", "other@test.local", "testpassword123", "Other User", "user")
    entry = create_credential(
        "user:other",
        "other-secret",
        value_plaintext="other-secret-value",
        created_by="other",
    )
    token = create_session("regular")["session_id"]

    reveal_r = test_client.post(
        f"/api/credentials/entries/{entry['id']}/reveal",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert reveal_r.status_code == 403
