"""Records close themselves, parallel sessions keep their own records, and history keeps its dates."""

from datetime import timedelta

from app.database import get_db
from app.services import activity_service, agent_service, workspace_service, workspace_sync_service
from app.services.session_context_service import build_session_context  # noqa: F401  (import check)
from app.time_utils import utc_now

WS = "workspace:s1-a"
_OWNER = {}


def _setup():
    with get_db() as conn:
        owner_id = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
    _OWNER["id"] = owner_id
    workspace_service.create_workspace("s1-a", "s1-a", owner_id)
    _, key = agent_service.create_agent(
        agent_id="s1-agent", display_name="S1", owner_user_id=owner_id, read_scopes=[WS], write_scopes=[WS]
    )
    return {"Authorization": f"Bearer {key}"}


def _mcp(client, headers, **params):
    return client.post("/mcp", headers=headers, json={"tool": "activity_update", "params": params})


def _execution():
    return workspace_sync_service.sync_workspace(agent_id="s1-agent", user_id=_OWNER["id"], memory_scope=WS)["execution_id"]


def _age(activity_id, **fields):
    with get_db() as conn:
        for column, delta in fields.items():
            conn.execute(
                f"UPDATE agent_activity SET {column} = ? WHERE id = ?",
                ((utc_now() - delta).isoformat(), activity_id),
            )
        conn.commit()


def test_completing_needs_a_task_result_over_mcp_and_rest(test_client, admin_token):
    headers = _setup()
    _mcp(test_client, headers, memory_scope=WS, task_description="Work")
    refused = _mcp(test_client, headers, memory_scope=WS, status="completed")
    assert refused.status_code == 400 and refused.json()["error"]["code"] == "TASK_RESULT_REQUIRED"
    task = activity_service.get_active_activity_for_agent("s1-agent", _OWNER["id"], memory_scope=WS)
    rest = test_client.put(f"/api/activity/{task['id']}", headers=headers, json={"status": "completed"})
    assert rest.status_code == 400 and rest.json()["error"]["code"] == "TASK_RESULT_REQUIRED"
    ok = _mcp(test_client, headers, memory_scope=WS, status="completed", task_result="Fixed the thing in commit abc123")
    assert ok.status_code == 200 and ok.json()["ok"], ok.text


def test_closing_a_blocked_record_later_keeps_the_time_its_work_ended(test_client, admin_token):
    headers = _setup()
    task = activity_service.create_activity(agent_id="s1-agent", user_id=_OWNER["id"], task_description="Old", memory_scope=WS)
    activity_service.update_activity(task["id"], status="blocked")
    _age(task["id"], ended_at=timedelta(days=20), updated_at=timedelta(days=20))
    before = activity_service.get_activity(task["id"])["ended_at"]
    r = test_client.put(
        f"/api/activity/{task['id']}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"status": "completed", "task_result": "closed out in clean-up"},
    )
    assert r.status_code == 200, r.text
    row = activity_service.get_activity(task["id"])
    assert row["status"] == "completed" and row["ended_at"] == before


def test_session_context_says_when_a_record_was_closed_out_long_after_its_work(test_client, admin_token):
    from app.services import session_context_service

    headers = _setup()
    task = activity_service.create_activity(agent_id="s1-agent", user_id=_OWNER["id"], task_description="Old work", memory_scope=WS)
    activity_service.update_activity(task["id"], status="blocked")
    _age(task["id"], ended_at=timedelta(days=20))
    activity_service.update_activity(task["id"], status="completed", task_result="done long ago")
    with get_db() as conn:
        rows = session_context_service._recent(conn, WS, set())
    assert rows and rows[0]["at"][:10] != rows[0]["updated_at"][:10]


def test_parallel_sessions_with_one_agent_key_keep_separate_records(test_client, admin_token):
    headers = _setup()
    first, second = _execution(), _execution()
    _mcp(test_client, headers, memory_scope=WS, task_description="Session one work", execution_id=first)
    _mcp(test_client, headers, memory_scope=WS, task_description="Session two work", execution_id=second)
    _mcp(test_client, headers, memory_scope=WS, task_note="one progress", execution_id=first)
    _mcp(test_client, headers, memory_scope=WS, task_note="two progress", execution_id=second)
    with get_db() as conn:
        rows = conn.execute(
            "SELECT task_description, task_note, source_execution_id FROM agent_activity WHERE assigned_agent_id = 's1-agent' ORDER BY started_at"
        ).fetchall()
    assert [(r["task_description"], r["task_note"]) for r in rows] == [
        ("Session one work", "one progress"),
        ("Session two work", "two progress"),
    ]


def test_a_new_session_takes_over_a_record_no_live_session_owns(test_client, admin_token):
    headers = _setup()
    old = _execution()
    _mcp(test_client, headers, memory_scope=WS, task_description="Left open", execution_id=old)
    task = activity_service.get_active_activity_for_agent("s1-agent", _OWNER["id"], memory_scope=WS)
    with get_db() as conn:  # the old session has been silent for an hour
        conn.execute(
            "UPDATE agent_executions SET last_seen_at = ? WHERE id = ?", ((utc_now() - timedelta(hours=1)).isoformat(), old)
        )
        conn.commit()
    new = _execution()
    _mcp(test_client, headers, memory_scope=WS, task_note="continuing", execution_id=new)
    row = activity_service.get_activity(task["id"])
    with get_db() as conn:
        owner = conn.execute("SELECT source_execution_id FROM agent_activity WHERE id = ?", (task["id"],)).fetchone()[0]
    assert row["task_note"] == "continuing" and owner == new


def test_an_open_task_nobody_updates_for_two_days_ends_without_report(test_client, admin_token):
    _setup()
    quiet = activity_service.create_activity(agent_id="s1-agent", user_id=_OWNER["id"], task_description="Quiet", memory_scope=WS)
    recent = activity_service.create_activity(agent_id="s1-agent", user_id=_OWNER["id"], task_description="Recent", memory_scope=WS)
    last_sign = utc_now() - timedelta(hours=60)
    with get_db() as conn:
        conn.execute("UPDATE agent_activity SET heartbeat_at = ? WHERE id = ?", (last_sign.isoformat(), quiet["id"]))
        conn.execute(
            "UPDATE agent_activity SET heartbeat_at = ? WHERE id = ?", ((utc_now() - timedelta(hours=47)).isoformat(), recent["id"])
        )
        conn.commit()
    assert activity_service.close_abandoned_activities() == 1
    row = activity_service.get_activity(quiet["id"])
    assert row["status"] == "cancelled" and row["ended_at"] == last_sign.isoformat()
    assert "Ended without report" in row["task_result"]
    assert activity_service.get_activity(recent["id"])["status"] in ("active", "stale")
    assert activity_service.close_abandoned_activities(after_hours=0) == 0  # 0 turns it off


def test_a_credential_request_and_the_task_blocked_on_it_are_one_row_for_the_person(test_client, admin_token):
    from app.services import credential_request_service, waiting_service

    headers = _setup()
    task = activity_service.create_activity(agent_id="s1-agent", user_id=_OWNER["id"], task_description="Needs a key", memory_scope=WS)
    request = credential_request_service.request_credential(
        agent_id="s1-agent", user_id=_OWNER["id"], scope=WS, name="deploy_token", purpose="to deploy"
    )
    activity_service.set_handoff(task["id"], {"needs_human": "Please add deploy_token"})
    activity_service.update_activity(task["id"], status="blocked")
    waiting = waiting_service.waiting_on_person()
    assert [r["name"] for r in waiting["credential_requests"]] == ["deploy_token"]
    assert task["id"] not in [h["id"] for h in waiting["handoffs"]]  # not a second row

    credential_request_service.fulfill(request["request_id"], value="s3cret", user_id=_OWNER["id"])
    row = activity_service.get_activity(task["id"])
    assert row["handoff_resolved_at"], "providing the credential answers the blocked task's ask"
    assert waiting_service.waiting_on_person()["credential_requests"] == []


def test_a_blocked_task_without_a_request_still_shows_for_the_person(test_client, admin_token):
    from app.services import waiting_service

    _setup()
    task = activity_service.create_activity(agent_id="s1-agent", user_id=_OWNER["id"], task_description="Decide", memory_scope=WS)
    activity_service.set_handoff(task["id"], {"needs_human": "Pick one"})
    activity_service.update_activity(task["id"], status="blocked")
    assert task["id"] in [h["id"] for h in waiting_service.waiting_on_person()["handoffs"]]
