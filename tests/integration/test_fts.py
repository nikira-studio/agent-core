import pytest


def test_memory_search_rejects_noise(test_client, agent_token):
    r = test_client.post(
        "/api/memory/search",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"query": "---"},
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["records"] == []


def test_memory_search_empty_query_rejected(test_client, agent_token):
    r = test_client.post(
        "/api/memory/search",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"query": ""},
    )
    assert r.status_code in (200, 400)
    if r.status_code == 200:
        assert r.json()["data"]["records"] == []


def test_memory_search_with_valid_query(test_client, agent_token):
    test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"content": "The workspace deadline is March 15", "memory_class": "fact", "scope": "agent:testagent"},
    )
    r = test_client.post(
        "/api/memory/search",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"query": "deadline"},
    )
    assert r.status_code == 200
    assert len(r.json()["data"]["records"]) >= 1


def test_memory_search_special_characters_handled(test_client, agent_token):
    r = test_client.post(
        "/api/memory/search",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"query": 'test"query'},
    )
    assert r.status_code == 200
