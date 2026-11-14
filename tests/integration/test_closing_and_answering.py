"""Closing a record that ended blocked."""

import json

from app.database import get_db
from app.services import activity_service, agent_service, memory_service, workspace_service

WS = "workspace:close-a"


def _setup():
    with get_db() as conn:
        owner_id = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
    workspace_service.create_workspace("close-a", "close-a", owner_id)
    _, owner_key = agent_service.create_agent(
        agent_id="close-owner", display_name="Owner", owner_user_id=owner_id,
        read_scopes=[WS], write_scopes=[WS],
    )
    _, other_key = agent_service.create_agent(
        agent_id="close-other", display_name="Other", owner_user_id=owner_id,
        read_scopes=[WS], write_scopes=[WS],
    )
    return {"Authorization": f"Bearer {owner_key}"}, {"Authorization": f"Bearer {other_key}"}


def _blocked(agent_id="close-owner"):
    activity = activity_service.create_activity(
        agent_id=agent_id, user_id="u", task_description="Ended blocked", memory_scope=WS
    )
    activity_service.update_activity(activity["id"], status="blocked")
    return activity["id"]


def _put(client, headers, activity_id, **body):
    return client.put(f"/api/activity/{activity_id}", headers=headers, json=body)


def test_the_owner_agent_can_close_its_own_blocked_record_with_a_reason(test_client, admin_token):
    owner, _ = _setup()
    activity_id = _blocked()
    r = _put(test_client, owner, activity_id, status="completed", task_result="work landed; closed 11-11")
    assert r.status_code == 200, r.text
    row = activity_service.get_activity(activity_id)
    assert row["status"] == "completed" and row["task_result"] == "work landed; closed 11-11"
    from app.services import audit_service

    events = audit_service.query_events(action="activity_update", limit=20)
    assert any(e["resource_id"] == activity_id and "blocked" in str(e) and "completed" in str(e) for e in events)


def test_an_admin_can_cancel_a_blocked_record_and_a_reason_is_required(test_client, admin_token):
    _setup()
    admin = {"Authorization": f"Bearer {admin_token}"}
    activity_id = _blocked()
    refused = _put(test_client, admin, activity_id, status="completed")
    assert refused.status_code == 400 and refused.json()["error"]["code"] == "TASK_RESULT_REQUIRED"
    assert activity_service.get_activity(activity_id)["status"] == "blocked"
    ok = _put(test_client, admin, activity_id, status="cancelled", task_result="no longer needed")
    assert ok.status_code == 200 and activity_service.get_activity(activity_id)["status"] == "cancelled"


def test_another_agent_cannot_close_someone_elses_blocked_record(test_client, admin_token):
    _, other = _setup()
    activity_id = _blocked()
    r = _put(test_client, other, activity_id, status="completed", task_result="not mine")
    assert r.status_code == 403
    assert activity_service.get_activity(activity_id)["status"] == "blocked"


def test_nothing_else_changes_state_after_a_record_has_ended(test_client, admin_token):
    owner, _ = _setup()
    admin = {"Authorization": f"Bearer {admin_token}"}
    done = activity_service.create_activity(agent_id="close-owner", user_id="u", task_description="Done", memory_scope=WS)
    activity_service.update_activity(done["id"], status="completed")
    for headers in (owner, admin):
        for status in ("completed", "cancelled", "blocked"):
            r = _put(test_client, headers, done["id"], status=status, task_result="again")
            assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_TRANSITION", (status, r.text)
    assert activity_service.get_activity(done["id"])["status"] == "completed"


# ---- suggestions answered by a check -----------------------------------------------------------------

def _mcp_update(client, headers, **params):
    return client.post("/mcp", headers=headers, json={"tool": "activity_update", "params": params})


def test_an_agent_can_close_its_own_blocked_task_through_the_mcp_tool(test_client, admin_token):
    owner, other = _setup()
    activity_id = _blocked()
    # Only the owner, only completed or cancelled, only with a reason, and nothing else rides along.
    assert not _mcp_update(test_client, other, activity_id=activity_id, status="completed", task_result="x").json()["ok"]
    assert not _mcp_update(test_client, owner, activity_id=activity_id, status="completed").json()["ok"]
    assert not _mcp_update(test_client, owner, activity_id=activity_id, status="active", task_result="x").json()["ok"]
    assert not _mcp_update(test_client, owner, activity_id=activity_id, status="completed", task_result="x", task_note="y").json()["ok"]
    assert activity_service.get_activity(activity_id)["status"] == "blocked"
    ok = _mcp_update(test_client, owner, activity_id=activity_id, status="completed", task_result="work landed")
    assert ok.json()["ok"], ok.text
    row = activity_service.get_activity(activity_id)
    assert row["status"] == "completed" and row["task_result"] == "work landed"
    again = _mcp_update(test_client, owner, activity_id=activity_id, status="cancelled", task_result="again")
    assert not again.json()["ok"]  # an ended record cannot be changed again
