import json


def _create(test_client, token, description, memory_scope=None):
    body = {"task_description": description}
    if memory_scope:
        body["memory_scope"] = memory_scope
    r = test_client.post(
        "/api/activity",
        headers={"Authorization": f"Bearer {token}"},
        json=body,
    )
    assert r.status_code == 201, f"create failed: {r.json()}"
    return r.json()["data"]["activity"]


def _complete(test_client, token, activity_id, task_result):
    r = test_client.put(
        f"/api/activity/{activity_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"status": "completed", "task_result": task_result},
    )
    assert r.status_code == 200, f"update failed: {r.json()}"


def test_rest_search_finds_work_by_description_and_result(test_client, agent_token):
    activity = _create(test_client, agent_token, "Fix the transmission adapter")
    _complete(test_client, agent_token, activity["id"], "Session handshake now retries on 409")
    _create(test_client, agent_token, "Unrelated docs pass")

    r = test_client.get(
        "/api/activity/search",
        headers={"Authorization": f"Bearer {agent_token}"},
        params={"query": "transmission"},
    )
    assert r.status_code == 200, r.json()
    hits = r.json()["data"]["activities"]
    assert [h["id"] for h in hits] == [activity["id"]]

    r2 = test_client.get(
        "/api/activity/search",
        headers={"Authorization": f"Bearer {agent_token}"},
        params={"query": "handshake"},
    )
    assert [h["id"] for h in r2.json()["data"]["activities"]] == [activity["id"]]


def test_rest_search_path_is_not_read_as_an_activity_id(test_client, agent_token):
    """GET /api/activity/search must not resolve as GET /api/activity/{id}."""
    r = test_client.get(
        "/api/activity/search",
        headers={"Authorization": f"Bearer {agent_token}"},
        params={"query": "anything"},
    )
    assert r.status_code == 200
    assert "activities" in r.json()["data"]


def test_rest_search_requires_auth(test_client):
    r = test_client.get("/api/activity/search", params={"query": "secret work"})
    assert r.status_code in (401, 403)


def test_rest_search_hides_unreadable_scopes(test_client, agent_token, admin_token):
    """An agent must not see trail entries from a scope it cannot read."""
    visible = _create(
        test_client, agent_token, "Indexed work in reach", memory_scope="agent:testagent"
    )

    from app.services import activity_service

    activity_service.create_activity(
        agent_id="otheragent",
        user_id="admin",
        task_description="Indexed work out of reach",
        memory_scope="workspace:not-authorized",
    )

    r = test_client.get(
        "/api/activity/search",
        headers={"Authorization": f"Bearer {agent_token}"},
        params={"query": "indexed"},
    )
    assert r.status_code == 200
    ids = [h["id"] for h in r.json()["data"]["activities"]]
    assert ids == [visible["id"]]


def test_mcp_manifest_advertises_activity_search(test_client, agent_token):
    r = test_client.get("/mcp", headers={"Authorization": f"Bearer {agent_token}"})
    assert r.status_code == 200
    assert "activity_search" in {t["name"] for t in r.json()["tools"]}


def test_mcp_activity_search_returns_matches(test_client, agent_token):
    activity = _create(
        test_client, agent_token, "Wire the searxng binding", memory_scope="agent:testagent"
    )

    r = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"tool": "activity_search", "params": {"query": "searxng"}},
    )
    assert r.status_code == 200, r.json()
    data = r.json()["data"]
    assert data["count"] == 1
    assert data["activities"][0]["id"] == activity["id"]


def test_mcp_activity_search_scopes_results(test_client, agent_token):
    from app.services import activity_service

    activity_service.create_activity(
        agent_id="otheragent",
        user_id="admin",
        task_description="Hidden firecrawl work",
        memory_scope="workspace:not-authorized",
    )

    r = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"tool": "activity_search", "params": {"query": "firecrawl"}},
    )
    assert r.status_code == 200
    assert r.json()["data"]["count"] == 0


def test_memory_write_cites_the_open_activity(test_client, agent_token):
    """A durable record should point back at the work that produced it."""
    activity = _create(
        test_client, agent_token, "Investigate the ranking bug", memory_scope="agent:testagent"
    )

    r = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "tool": "memory_write",
            "params": {
                "content": "Importance is self-reported and clusters high, so it cannot rank.",
                "memory_class": "fact",
                "scope": "agent:testagent",
            },
        },
    )
    assert r.status_code == 201, r.json()
    record_id = r.json()["data"]["record"]["id"]

    from app.database import get_db

    with get_db() as conn:
        row = conn.execute(
            "SELECT provenance_json FROM memory_records WHERE id = ?", (record_id,)
        ).fetchone()

    provenance = json.loads(row["provenance_json"])
    assert provenance["activity_id"] == activity["id"]


def test_memory_write_without_an_activity_omits_the_citation(test_client, agent_token):
    r = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "tool": "memory_write",
            "params": {
                "content": "Written with no activity open.",
                "memory_class": "fact",
                "scope": "agent:testagent",
            },
        },
    )
    assert r.status_code == 201, r.json()

    from app.database import get_db

    with get_db() as conn:
        row = conn.execute(
            "SELECT provenance_json FROM memory_records WHERE id = ?",
            (r.json()["data"]["record"]["id"],),
        ).fetchone()

    assert "activity_id" not in json.loads(row["provenance_json"])
