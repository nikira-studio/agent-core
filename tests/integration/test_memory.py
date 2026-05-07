import pytest


def test_memory_write(test_client, agent_token):
    r = test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "content": "The workspace deadline is March 15",
            "memory_class": "fact",
            "scope": "agent:testagent",
        },
    )
    assert r.status_code == 201, f"write failed: {r.json()}"
    assert r.json()["ok"] == True


def test_memory_write_rejects_removed_classes(test_client, agent_token):
    r = test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "content": "This should not be accepted",
            "memory_class": "opinion",
            "scope": "agent:testagent",
        },
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_CLASS"


def test_memory_write_rejects_invalid_source_kind(test_client, agent_token):
    r = test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "content": "This should not be accepted",
            "memory_class": "fact",
            "source_kind": "user_assertion",
            "scope": "agent:testagent",
        },
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_SOURCE_KIND"


def test_memory_write_rejects_invalid_confidence_and_importance(test_client, agent_token):
    confidence = test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "content": "Invalid confidence",
            "memory_class": "fact",
            "scope": "agent:testagent",
            "confidence": 1.5,
        },
    )
    assert confidence.status_code == 400
    assert confidence.json()["error"]["code"] == "INVALID_CONFIDENCE"

    importance = test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "content": "Invalid importance",
            "memory_class": "fact",
            "scope": "agent:testagent",
            "importance": -0.1,
        },
    )
    assert importance.status_code == 400
    assert importance.json()["error"]["code"] == "INVALID_IMPORTANCE"


def test_memory_search(test_client, agent_token):
    test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "content": "Python is the best language",
            "memory_class": "fact",
            "scope": "agent:testagent",
        },
    )
    r = test_client.post(
        "/api/memory/search",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"query": "Python", "limit": 10},
    )
    assert r.status_code == 200, f"search failed: {r.json()}"
    assert "records" in r.json()["data"]


def test_memory_search_rejects_removed_classes(test_client, agent_token):
    r = test_client.post(
        "/api/memory/search",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"query": "workspace", "memory_class": "belief"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_CLASS"


def test_memory_get(test_client, agent_token):
    write_r = test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "content": "Test memory",
            "memory_class": "fact",
            "scope": "agent:testagent",
        },
    )
    assert write_r.status_code == 201
    r = test_client.post(
        "/api/memory/get",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"scope": "agent:testagent", "limit": 10},
    )
    assert r.status_code == 200
    assert len(r.json()["data"]["records"]) >= 1


def test_memory_search_scope_filter_limits_results(test_client, admin_token):
    user_r = test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "content": "Scoped dashboard search token in user scope",
            "memory_class": "fact",
            "scope": "user:admin",
        },
    )
    assert user_r.status_code == 201, user_r.json()

    agent_r = test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "content": "Scoped dashboard search token in agent scope",
            "memory_class": "fact",
            "scope": "agent:testagent",
        },
    )
    assert agent_r.status_code == 201, agent_r.json()

    r = test_client.post(
        "/api/memory/search",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"query": "Scoped dashboard search token", "scope": "agent:testagent"},
    )
    assert r.status_code == 200, r.json()
    records = r.json()["data"]["records"]
    assert records
    assert all(record["scope"] == "agent:testagent" for record in records)


def test_memory_search_uses_domain_topic_confidence_and_importance(test_client, admin_token):
    records_to_write = [
        {
            "content": "Memory field behavior ranking token",
            "memory_class": "fact",
            "scope": "user:admin",
            "domain": "engineering",
            "topic": "docker",
            "confidence": 0.4,
            "importance": 1.0,
        },
        {
            "content": "Memory field behavior ranking token",
            "memory_class": "fact",
            "scope": "user:admin",
            "domain": "engineering",
            "topic": "docker",
            "confidence": 0.9,
            "importance": 0.2,
        },
        {
            "content": "Memory field behavior ranking token",
            "memory_class": "fact",
            "scope": "user:admin",
            "domain": "personal",
            "topic": "docker",
            "confidence": 1.0,
            "importance": 1.0,
        },
    ]
    for payload in records_to_write:
        r = test_client.post(
            "/api/memory/write",
            headers={"Authorization": f"Bearer {admin_token}"},
            json=payload,
        )
        assert r.status_code == 201, r.json()

    filtered = test_client.post(
        "/api/memory/search",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "query": "ranking token",
            "scope": "user:admin",
            "domain": "engineering",
            "topic": "docker",
            "min_confidence": 0.8,
        },
    )
    assert filtered.status_code == 200, filtered.json()
    filtered_records = filtered.json()["data"]["records"]
    assert len(filtered_records) == 1
    assert filtered_records[0]["confidence"] == 0.9
    assert filtered_records[0]["domain"] == "engineering"
    assert filtered_records[0]["topic"] == "docker"

    ranked = test_client.post(
        "/api/memory/search",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "query": "ranking token",
            "scope": "user:admin",
            "domain": "engineering",
            "topic": "docker",
        },
    )
    assert ranked.status_code == 200, ranked.json()
    ranked_records = ranked.json()["data"]["records"]
    assert len(ranked_records) == 2
    assert ranked_records[0]["importance"] == 1.0
    assert ranked_records[1]["importance"] == 0.2


def test_memory_detail_endpoint_enforces_scope(test_client, admin_token, agent_token):
    write_r = test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "content": "Private detail record",
            "memory_class": "fact",
            "scope": "agent:testagent",
            "domain": "validation",
            "topic": "detail",
        },
    )
    assert write_r.status_code == 201
    record_id = write_r.json()["data"]["record"]["id"]

    own_r = test_client.get(
        f"/api/memory/{record_id}",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert own_r.status_code == 200
    assert own_r.json()["data"]["record"]["content"] == "Private detail record"

    other_agent = test_client.post(
        "/api/agents",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"id": "otheragent", "display_name": "Other Agent"},
    )
    assert other_agent.status_code == 201
    other_rotate = test_client.post(
        "/api/agents/otheragent/rotate_key",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert other_rotate.status_code == 200
    other_token = other_rotate.json()["data"]["api_key"]

    denied_r = test_client.get(
        f"/api/memory/{record_id}",
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert denied_r.status_code == 403


def test_memory_retract(test_client, agent_token):
    write_r = test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "content": "To be retracted",
            "memory_class": "fact",
            "scope": "agent:testagent",
        },
    )
    assert write_r.status_code == 201
    record_id = write_r.json()["data"]["record"]["id"]
    r = test_client.post(
        f"/api/memory/retract?record_id={record_id}",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert r.status_code == 200


def test_memory_retract_accepts_json_body_and_uses_correct_error_code(test_client, agent_token):
    write_r = test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "content": "Retract from body",
            "memory_class": "fact",
            "scope": "agent:testagent",
        },
    )
    assert write_r.status_code == 201
    record_id = write_r.json()["data"]["record"]["id"]

    r = test_client.post(
        "/api/memory/retract",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"record_id": record_id},
    )
    assert r.status_code == 200

    r2 = test_client.post(
        "/api/memory/retract",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"record_id": record_id},
    )
    assert r2.status_code == 400
    assert r2.json()["error"]["code"] == "ALREADY_RETRACTED"


def test_pii_gate_blocks_shared_scope(test_client, agent_token):
    r = test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "content": "Contact me at alice@example.com",
            "memory_class": "fact",
            "scope": "shared",
        },
    )
    assert r.status_code in (403, 422)


def test_supersession_chain(test_client, agent_token):
    r1 = test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "content": "Original fact",
            "memory_class": "fact",
            "scope": "agent:testagent",
        },
    )
    assert r1.status_code == 201
    record1_id = r1.json()["data"]["record"]["id"]
    r2 = test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "content": "Updated fact",
            "memory_class": "fact",
            "scope": "agent:testagent",
            "supersedes_id": record1_id,
        },
    )
    assert r2.status_code == 201
    assert r2.json()["data"]["record"]["supersedes_id"] == record1_id
    record2_id = r2.json()["data"]["record"]["id"]

    chain_r = test_client.get(
        f"/api/memory/{record2_id}/chain",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert chain_r.status_code == 200
    chain = chain_r.json()["data"]["chain"]
    assert [r["content"] for r in chain] == ["Original fact", "Updated fact"]
