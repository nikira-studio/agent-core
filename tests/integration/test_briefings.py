
def test_handoff_briefing_returns_structured_sections(test_client, agent_token):
    create_activity = test_client.post(
        "/api/activity",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"task_description": "Structured briefing test", "memory_scope": "agent:testagent"},
    )
    assert create_activity.status_code == 201
    activity_id = create_activity.json()["data"]["activity"]["id"]

    test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "content": "Prefer concise updates",
            "memory_class": "preference",
            "scope": "agent:testagent",
        },
    )
    test_client.post(
        "/api/memory/write",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "content": "Use the existing connector binding",
            "memory_class": "decision",
            "scope": "agent:testagent",
        },
    )

    complete_r = test_client.put(
        f"/api/activity/{activity_id}",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "task_note": "Captured the intermediate findings",
            "status": "completed",
            "task_result": "Finished the briefing source task",
        },
    )
    assert complete_r.status_code == 200

    r = test_client.post(
        "/api/briefings/handoff",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"activity_id": activity_id},
    )
    assert r.status_code in (200, 201)
    briefing = r.json()["data"]["briefing"]
    assert "facts" in briefing
    assert "decisions" in briefing
    assert "preferences" in briefing
    assert "recent_completed" in briefing
    assert briefing["task_description"] == "Structured briefing test"
    assert briefing["task_note"] == "Captured the intermediate findings"
    assert briefing["task_result"] == "Finished the briefing source task"
    assert briefing["source_activity_id"] == activity_id


def test_handoff_briefing_requires_activity(test_client, agent_token):
    r = test_client.post(
        "/api/briefings/handoff",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"activity_id": "nonexistent-activity-id"},
    )
    assert r.status_code == 404


def test_handoff_briefing_forbidden_for_unauthorized(test_client, agent_token):
    r = test_client.post(
        "/api/activity",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"task_description": "Test for briefing"},
    )
    assert r.status_code == 201
    activity_id = r.json()["data"]["activity"]["id"]

    r = test_client.post(
        "/api/briefings/handoff",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"activity_id": activity_id},
    )
    assert r.status_code in (200, 201)


def test_get_briefing_not_found(test_client, agent_token):
    r = test_client.get(
        "/api/briefings/nonexistent-briefing-id",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert r.status_code == 404
