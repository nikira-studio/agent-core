

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


def test_memory_search_matches_topic_as_well_as_content(test_client, agent_token):
    """Regression test: the FTS table indexes content AND topic (the triggers
    maintain both), but the search SQL used `fts.content MATCH`,
    column-restricting to content — so a record could never be found by its own
    topic. That broke the documented recall workflow ("retry with exact topic
    values") and mixed queries where one token only appears in the topic (e.g.
    content mentions pgvector, topic is "database", query "pgvector database" →
    zero results under AND semantics)."""
    r = test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "content": "DECISION: We use PostgreSQL 16 with pgvector for embeddings.",
            "memory_class": "decision",
            "scope": "agent:testagent",
            "topic": "database",
        },
    )
    assert r.status_code == 201, r.json()
    record_id = r.json()["data"]["record"]["id"]

    for query in ("database", "pgvector database"):
        r = test_client.post(
            "/api/memory/search",
            headers={"Authorization": f"Bearer {agent_token}"},
            json={"query": query},
        )
        assert r.status_code == 200
        ids = {rec["id"] for rec in r.json()["data"]["records"]}
        assert record_id in ids, f"query {query!r} should find the record via its topic"
