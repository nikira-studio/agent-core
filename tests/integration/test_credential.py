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
