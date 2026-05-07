import pytest
from fastapi.testclient import TestClient


def test_quickstart_sequence(test_client, clean_db):
    r = test_client.get("/health")
    assert r.status_code == 200

    r = test_client.post("/api/auth/register", json={
        "email": "admin@test.local",
        "password": "testpassword123",
        "display_name": "Admin",
    })
    assert r.status_code == 200

    r = test_client.post("/api/auth/login", json={
        "email": "admin@test.local",
        "password": "testpassword123",
    })
    assert r.status_code == 200
    session_id = r.json()["data"]["session_id"]

    r = test_client.post(
        "/api/agents",
        headers={"Authorization": f"Bearer {session_id}"},
        json={"id": "quickstart-agent", "display_name": "Quickstart Agent"},
    )
    assert r.status_code == 201

    r = test_client.post(
        "/api/agent-setup/generate-connection",
        headers={"Authorization": f"Bearer {session_id}"},
        json={"user_id": "admin", "agent_id": "quickstart-agent", "output_type": "env"},
    )
    assert r.status_code == 200
    agent_api_key = r.json()["data"]["api_key"]

    r = test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {agent_api_key}"},
        json={
            "content": "Project deadline is March 15",
            "memory_class": "fact",
            "scope": "agent:quickstart-agent",
        },
    )
    assert r.status_code == 201

    r = test_client.post(
        "/api/vault/entries",
        headers={"Authorization": f"Bearer {agent_api_key}"},
        json={
            "scope": "agent:quickstart-agent",
            "name": "api-key-1",
            "value": "EXAMPLE_TEST_API_KEY",
            "value_type": "api",
        },
    )
    assert r.status_code == 201
    entry_id = r.json()["data"]["entry"]["id"]

    r = test_client.post(
        f"/api/vault/entries/{entry_id}/reference",
        headers={"Authorization": f"Bearer {agent_api_key}"},
    )
    assert r.status_code == 200

    r = test_client.post(
        "/api/memory/search",
        headers={"Authorization": f"Bearer {agent_api_key}"},
        json={"query": "deadline", "limit": 5},
    )
    assert r.status_code == 200
    assert "records" in r.json()["data"]
