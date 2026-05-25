
import json


def test_create_activity(test_client, agent_token):
    r = test_client.post(
        "/api/activity",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"task_description": "Complete Q1 report", "memory_scope": "agent:testagent"},
    )
    assert r.status_code == 201, f"create failed: {r.json()}"
    data = r.json()["data"]
    assert data["activity"]["task_description"] == "Complete Q1 report"
    assert data["activity"]["status"] == "active"

    from app.database import get_db

    with get_db() as conn:
        row = conn.execute(
            "SELECT details_json FROM audit_log WHERE action = 'activity_update' AND resource_id = ? ORDER BY id DESC LIMIT 1",
            (data["activity"]["id"],),
        ).fetchone()

    assert row is not None
    details = json.loads(row["details_json"])
    assert details["action"] == "create"
    assert details["task_description"] == "Complete Q1 report"
    assert details["memory_scope"] == "agent:testagent"


def test_list_activities(test_client, agent_token):
    test_client.post(
        "/api/activity",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"task_description": "Test activity"},
    )
    r = test_client.get(
        "/api/activity",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert r.status_code == 200
    assert len(r.json()["data"]["activities"]) >= 1


def test_update_activity_status(test_client, agent_token):
    create_r = test_client.post(
        "/api/activity",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"task_description": "Status update test"},
    )
    assert create_r.status_code == 201
    activity_id = create_r.json()["data"]["activity"]["id"]

    r = test_client.put(
        f"/api/activity/{activity_id}",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"status": "stale"},
    )
    assert r.status_code == 200
    assert r.json()["data"]["activity"]["status"] == "stale"

    from app.database import get_db

    with get_db() as conn:
        row = conn.execute(
            "SELECT details_json FROM audit_log WHERE action = 'activity_update' AND resource_id = ? ORDER BY id DESC LIMIT 1",
            (activity_id,),
        ).fetchone()

    assert row is not None
    details = json.loads(row["details_json"])
    assert details["action"] == "update"
    assert details["previous_status"] == "active"
    assert details["new_status"] == "stale"
    assert "task_result" not in details


def test_update_activity_can_store_task_result(test_client, agent_token):
    create_r = test_client.post(
        "/api/activity",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"task_description": "Completion result test"},
    )
    assert create_r.status_code == 201
    activity_id = create_r.json()["data"]["activity"]["id"]

    r = test_client.put(
        f"/api/activity/{activity_id}",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"status": "completed", "task_result": "Finished the requested update"},
    )
    assert r.status_code == 200
    activity = r.json()["data"]["activity"]
    assert activity["status"] == "completed"
    assert activity["task_result"] == "Finished the requested update"

    from app.database import get_db

    with get_db() as conn:
        row = conn.execute(
            "SELECT details_json FROM audit_log WHERE action = 'activity_update' AND resource_id = ? ORDER BY id DESC LIMIT 1",
            (activity_id,),
        ).fetchone()

    assert row is not None
    details = json.loads(row["details_json"])
    assert details["new_status"] == "completed"
    assert details["task_result"] == "Finished the requested update"


def test_update_activity_can_store_task_note(test_client, agent_token):
    create_r = test_client.post(
        "/api/activity",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"task_description": "Progress note test"},
    )
    assert create_r.status_code == 201
    activity_id = create_r.json()["data"]["activity"]["id"]

    r = test_client.put(
        f"/api/activity/{activity_id}",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"task_note": "Wrote the first draft"},
    )
    assert r.status_code == 200
    activity = r.json()["data"]["activity"]
    assert activity["task_note"] == "Wrote the first draft"

    from app.database import get_db

    with get_db() as conn:
        row = conn.execute(
            "SELECT details_json FROM audit_log WHERE action = 'activity_update' AND resource_id = ? ORDER BY id DESC LIMIT 1",
            (activity_id,),
        ).fetchone()

    assert row is not None
    details = json.loads(row["details_json"])
    assert details["task_note"] == "Wrote the first draft"


def test_activity_requires_agent(test_client, admin_token):
    r = test_client.post(
        "/api/activity",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"task_description": "No agent context"},
    )
    assert r.status_code == 400


def test_admin_can_create_activity_for_assigned_agent(test_client, admin_token):
    create_agent = test_client.post(
        "/api/agents",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"id": "dashboardagent", "display_name": "Dashboard Agent"},
    )
    assert create_agent.status_code == 201, f"agent create failed: {create_agent.json()}"

    r = test_client.post(
        "/api/activity",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "assigned_agent_id": "dashboardagent",
            "task_description": "Dashboard-created activity",
            "memory_scope": "agent:dashboardagent",
        },
    )
    assert r.status_code == 201, f"admin activity create failed: {r.json()}"
    activity = r.json()["data"]["activity"]
    assert activity["agent_id"] == "dashboardagent"
    assert activity["assigned_agent_id"] == "dashboardagent"


def test_heartbeat_activity(test_client, agent_token):
    create_r = test_client.post(
        "/api/activity",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"task_description": "Heartbeat test"},
    )
    assert create_r.status_code == 201
    activity_id = create_r.json()["data"]["activity"]["id"]

    r = test_client.post(
        f"/api/activity/{activity_id}/heartbeat",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert r.status_code == 200
