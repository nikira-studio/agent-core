import pytest
from unittest.mock import patch


def test_vault_create_entry(test_client, agent_token):
    r = test_client.post(
        "/api/vault/entries",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "scope": "agent:testagent",
            "name": "test-api-key",
            "value": "EXAMPLE_LIVE_API_KEY",
            "label": "Production API Key",
            "value_type": "api",
        },
    )
    assert r.status_code == 201, f"create failed: {r.json()}"
    data = r.json()["data"]
    assert data["entry"]["reference_name"].startswith("AC_SECRET_TEST_API_KEY_")


def test_vault_rejects_invalid_type_enums(test_client, agent_token):
    invalid_value_type = test_client.post(
        "/api/vault/entries",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "scope": "agent:testagent",
            "name": "invalid-value-type",
            "value": "secret",
            "value_type": "magic",
        },
    )
    assert invalid_value_type.status_code == 400
    assert invalid_value_type.json()["error"]["code"] == "INVALID_VALUE_TYPE"


def test_vault_rejects_empty_name_on_update(test_client, agent_token):
    create_r = test_client.post(
        "/api/vault/entries",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"scope": "agent:testagent", "name": "update-test", "value": "secret"},
    )
    assert create_r.status_code == 201
    entry_id = create_r.json()["data"]["entry"]["id"]

    update_r = test_client.put(
        f"/api/vault/entries/{entry_id}",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"name": ""},
    )
    assert update_r.status_code == 400
    assert update_r.json()["error"]["code"] == "INVALID_NAME"


def test_vault_list_entries(test_client, agent_token):
    test_client.post(
        "/api/vault/entries",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"scope": "agent:testagent", "name": "list-test", "value": "secret"},
    )
    r = test_client.get(
        "/api/vault/entries?scope=agent:testagent",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert r.status_code == 200
    assert len(r.json()["data"]["entries"]) >= 1


def test_vault_reveal_requires_otp(test_client, agent_token):
    create_r = test_client.post(
        "/api/vault/entries",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"scope": "agent:testagent", "name": "reveal-test", "value": "secret-value"},
    )
    assert create_r.status_code == 201
    entry_id = create_r.json()["data"]["entry"]["id"]
    r = test_client.post(
        f"/api/vault/entries/{entry_id}/reveal",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"otp_code": "000000"},
    )
    assert r.status_code == 403


def test_vault_reference(test_client, agent_token):
    create_r = test_client.post(
        "/api/vault/entries",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"scope": "agent:testagent", "name": "ref-test", "value": "credential-data"},
    )
    assert create_r.status_code == 201
    entry_id = create_r.json()["data"]["entry"]["id"]
    r = test_client.post(
        f"/api/vault/entries/{entry_id}/reference",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert r.status_code == 200
    assert "reference_name" in r.json()["data"]


def test_vault_scopes(test_client, agent_token):
    r = test_client.get(
        "/api/vault/scopes",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert r.status_code == 200


def test_non_admin_user_can_reveal_own_vault_entry_with_otp(test_client, clean_db):
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
        "/api/vault/entries",
        headers={"Authorization": f"Bearer {token}"},
        json={"scope": "user:regular", "name": "own-secret", "value": "user-secret"},
    )
    assert create_r.status_code == 201, create_r.json()
    entry_id = create_r.json()["data"]["entry"]["id"]

    with patch("app.services.auth_service.verify_otp", return_value=True):
        reveal_r = test_client.post(
            f"/api/vault/entries/{entry_id}/reveal",
            headers={"Authorization": f"Bearer {token}"},
            json={"otp_code": "123456"},
        )

    assert reveal_r.status_code == 200, reveal_r.json()
    assert reveal_r.json()["data"]["value"] == "user-secret"


def test_non_admin_user_cannot_reveal_other_user_vault_entry(test_client, clean_db):
    from app.services.auth_service import create_user, create_session
    from app.services.vault_service import create_vault_entry

    create_user("regular", "regular@test.local", "testpassword123", "Regular User", "user")
    create_user("other", "other@test.local", "testpassword123", "Other User", "user")
    entry = create_vault_entry("user:other", "other-secret", "other-secret-value", created_by="other")
    token = create_session("regular")["session_id"]

    with patch("app.services.auth_service.verify_otp", return_value=True):
        reveal_r = test_client.post(
            f"/api/vault/entries/{entry['id']}/reveal",
            headers={"Authorization": f"Bearer {token}"},
            json={"otp_code": "123456"},
        )

    assert reveal_r.status_code == 403
