import pytest


def test_broker_resolve_requires_auth(test_client):
    r = test_client.post(
        "/internal/vault/resolve",
        json={"variable_name": "AC_SECRET_TEST", "agent_id": "testagent"},
    )
    assert r.status_code == 401


def test_broker_resolve_rejects_bearer_token(test_client, agent_token):
    r = test_client.post(
        "/internal/vault/resolve",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"variable_name": "AC_SECRET_TEST", "agent_id": "testagent"},
    )
    assert r.status_code == 401
