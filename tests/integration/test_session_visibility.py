"""Sessions are visible as sessions: activities name the session that wrote them, and a session start says when
another session of the same agent is live in the workspace."""

from app.database import get_db
from app.services import activity_service, agent_service, workspace_service

SCOPE = "workspace:vis-test"


def _setup(admin_token):
    with get_db() as conn:
        owner_id = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
    workspace_service.create_workspace("vis-test", "Visibility Test", owner_id)
    _, issued = agent_service.create_agent(
        agent_id="vis-agent", display_name="Vis Agent", owner_user_id=owner_id,
        read_scopes=[SCOPE], write_scopes=[SCOPE],
    )
    return {"Authorization": f"Bearer {issued}"}, owner_id


def _start(client, headers, ref):
    response = client.post(
        "/api/session-context", headers=headers, json={"memory_scope": SCOPE, "host_session_ref": ref}
    )
    assert response.status_code == 200, response.json()
    return response.json()["data"]


def test_activity_reads_name_the_session_that_wrote_them(test_client, admin_token):
    headers, owner = _setup(admin_token)
    session = _start(test_client, headers, "thread-A")["execution_id"]
    written = activity_service.create_activity(
        agent_id="vis-agent", user_id=owner, task_description="Write the report",
        memory_scope=SCOPE, source_execution_id=session,
    )
    bare = activity_service.create_activity(
        agent_id="vis-agent", user_id=owner, task_description="Written with no session", memory_scope=SCOPE,
    )

    got = activity_service.get_activity(written["id"])
    assert got["source_execution_id"] == session and got["host_session_ref"] == "thread-A"
    compact = activity_service.compact_activity(got)
    assert compact["session"] == {"execution_id": session, "host_session_ref": "thread-A"}

    listed = {a["id"]: a for a in activity_service.list_activities(user_id=owner)}
    assert listed[written["id"]]["host_session_ref"] == "thread-A"
    assert listed[bare["id"]]["host_session_ref"] is None
    assert "session" not in activity_service.compact_activity(listed[bare["id"]])

    response = test_client.get(f"/api/activity/{written['id']}", headers=headers)
    assert response.status_code == 200
    body = response.json()["data"]
    assert (body.get("activity") or body)["host_session_ref"] == "thread-A"


def test_session_context_names_the_session_on_open_work(test_client, admin_token):
    headers, owner = _setup(admin_token)
    first = _start(test_client, headers, "thread-A")
    activity_service.create_activity(
        agent_id="vis-agent", user_id=owner, task_description="Draft the plan",
        memory_scope=SCOPE, source_execution_id=first["execution_id"],
    )
    again = _start(test_client, headers, "thread-A")  # the same session starts again (clear or compact)
    assert f"[session {first['execution_id'][:8]}, this one]" in again["context"]

    other = _start(test_client, headers, "thread-B")
    assert f"[session {first['execution_id'][:8]}]" in other["context"]
    assert "this one" not in other["context"].split("## Open work")[1].split("##")[0]


def test_a_second_live_session_of_the_same_agent_is_noted(test_client, admin_token):
    headers, _ = _setup(admin_token)
    first = _start(test_client, headers, "thread-A")
    assert "Other sessions of" not in first["context"]

    second = _start(test_client, headers, "thread-B")
    text = second["context"]
    assert "## Other sessions of vis-agent in this workspace (1)" in text
    assert f"session {first['execution_id'][:8]}, last seen 0 min ago" in text
    assert "do not assume one person is watching all of them" in text
    assert second["execution_id"][:8] not in text.split("## Other sessions")[1].split("\n\n")[0]


def test_the_same_session_starting_again_is_not_another_session(test_client, admin_token):
    headers, _ = _setup(admin_token)
    _start(test_client, headers, "thread-A")
    again = _start(test_client, headers, "thread-A")
    assert "Other sessions of" not in again["context"]


def test_a_session_idle_past_the_stale_window_is_not_listed(test_client, admin_token):
    headers, _ = _setup(admin_token)
    first = _start(test_client, headers, "thread-A")
    with get_db() as conn:
        conn.execute(
            "UPDATE agent_executions SET last_seen_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (first["execution_id"],),
        )
        conn.commit()
    second = _start(test_client, headers, "thread-B")
    assert "Other sessions of" not in second["context"]


def test_an_ended_session_is_not_listed(test_client, admin_token):
    headers, _ = _setup(admin_token)
    first = _start(test_client, headers, "thread-A")
    with get_db() as conn:
        conn.execute("UPDATE agent_executions SET status = 'ended' WHERE id = ?", (first["execution_id"],))
        conn.commit()
    second = _start(test_client, headers, "thread-B")
    assert "Other sessions of" not in second["context"]
