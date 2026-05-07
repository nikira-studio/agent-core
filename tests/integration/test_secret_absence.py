import pytest


def test_vault_entry_response_no_raw_secret(test_client, agent_token):
    r = test_client.post(
        "/api/vault/entries",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "scope": "agent:testagent",
            "name": "raw-secret-test",
            "value": "super_secret_api_key_12345",
            "value_type": "api",
        },
    )
    assert r.status_code == 201
    data = r.json()["data"]["entry"]
    assert "value" not in data
    assert "value_encrypted" not in data
    assert data["name"] == "raw-secret-test"
    assert "reference_name" in data


def test_vault_list_no_raw_secrets(test_client, agent_token):
    test_client.post(
        "/api/vault/entries",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"scope": "agent:testagent", "name": "list-secret", "value": "secret_value_xyz"},
    )
    r = test_client.get(
        "/api/vault/entries?scope=agent:testagent",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert r.status_code == 200
    for entry in r.json()["data"]["entries"]:
        assert "value" not in entry
        assert "value_encrypted" not in entry


def test_memory_write_no_raw_secrets_in_response(test_client, agent_token):
    secret_content = "User API key is EXAMPLE_LIVE_KEY_ABC123"
    r = test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"content": secret_content, "memory_class": "fact", "scope": "agent:testagent"},
    )
    assert r.status_code == 201
    data = r.json()["data"]["record"]
    assert data["content"] == secret_content
