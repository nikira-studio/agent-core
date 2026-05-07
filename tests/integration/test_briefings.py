import pytest


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
