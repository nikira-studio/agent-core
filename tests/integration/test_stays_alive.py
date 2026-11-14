"""A task that is being reported on is alive: notes, results and heartbeats keep it (or bring it back) active."""

from datetime import timedelta

from app.database import get_db
from app.services import activity_service, agent_service, workspace_service
from app.time_utils import utc_now

WS = "workspace:alive-a"
_OWNER = {}


def _setup():
    with get_db() as conn:
        owner_id = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
    workspace_service.create_workspace("alive-a", "alive-a", owner_id)
    _, key = agent_service.create_agent(
        agent_id="alive-owner", display_name="Owner", owner_user_id=owner_id,
        read_scopes=[WS], write_scopes=[WS],
    )
    _OWNER["id"] = owner_id
    return {"Authorization": f"Bearer {key}"}


def _old(activity_id, minutes=30):
    """Make the record look as if its last sign of life was `minutes` ago."""
    old = (utc_now() - timedelta(minutes=minutes)).isoformat()
    with get_db() as conn:
        conn.execute("UPDATE agent_activity SET heartbeat_at = ?, started_at = ? WHERE id = ?", (old, old, activity_id))
        conn.commit()
    return old


def _mcp(client, headers, **params):
    r = client.post(
        "/mcp",
        headers=headers,
        json={"tool": "activity_update", "params": params},
    )
    assert r.status_code == 200 and r.json()["ok"], r.text
    return r


def _task():
    return activity_service.create_activity(
        agent_id="alive-owner", user_id=_OWNER["id"], task_description="Long task", memory_scope=WS
    )


def test_a_progress_note_refreshes_the_heartbeat(test_client, admin_token):
    headers = _setup()
    task = _task()
    old = _old(task["id"])
    _mcp(test_client, headers, memory_scope=WS, task_note="halfway there")
    row = activity_service.get_activity(task["id"])
    assert row["heartbeat_at"] > old and row["status"] == "active"
    assert activity_service.mark_stale_activities() == 0


def test_a_note_on_a_stale_record_brings_it_back_to_active(test_client, admin_token):
    headers = _setup()
    task = _task()
    _old(task["id"])
    assert activity_service.mark_stale_activities() >= 1
    assert activity_service.get_activity(task["id"])["status"] == "stale"
    _mcp(test_client, headers, memory_scope=WS, task_note="still working")
    row = activity_service.get_activity(task["id"])
    assert row["status"] == "active"
    assert activity_service.mark_stale_activities() == 0


def test_an_empty_heartbeat_brings_a_stale_record_back(test_client, admin_token):
    headers = _setup()
    task = _task()
    _old(task["id"])
    activity_service.mark_stale_activities()
    _mcp(test_client, headers, memory_scope=WS)
    assert activity_service.get_activity(task["id"])["status"] == "active"


def test_recovery_is_announced_once_with_activity_recovered(test_client, admin_token, monkeypatch):
    headers = _setup()
    task = _task()
    _old(task["id"])
    activity_service.mark_stale_activities()
    seen = []
    monkeypatch.setattr(activity_service, "notify", lambda event, activity, **extra: seen.append(event) or {})
    _mcp(test_client, headers, memory_scope=WS, task_note="back")
    _mcp(test_client, headers, memory_scope=WS, task_note="again")
    assert seen == ["activity_recovered", "activity_updated"]


def test_rest_heartbeat_on_a_stale_record_succeeds(test_client, admin_token):
    headers = _setup()
    task = _task()
    _old(task["id"])
    activity_service.mark_stale_activities()
    r = test_client.post(f"/api/activity/{task['id']}/heartbeat", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["activity"]["status"] == "active"


def test_an_admin_edit_is_not_a_sign_of_life(test_client, admin_token):
    _setup()
    task = _task()
    old = _old(task["id"])
    activity_service.mark_stale_activities()
    admin = {"Authorization": f"Bearer {admin_token}"}
    r = test_client.put(f"/api/activity/{task['id']}", headers=admin, json={"task_note": "admin comment"})
    assert r.status_code == 200, r.text
    row = activity_service.get_activity(task["id"])
    assert row["status"] == "stale" and row["heartbeat_at"] == old


def test_an_ended_record_is_not_revived_by_a_note(test_client, admin_token):
    _setup()
    task = _task()
    activity_service.update_activity(task["id"], status="completed")
    activity_service.update_activity(task["id"], task_note="late", alive=True)
    assert activity_service.get_activity(task["id"])["status"] == "completed"
