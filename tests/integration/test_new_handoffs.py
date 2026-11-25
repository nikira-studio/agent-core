"""A session can ask whether another session closed a handoff in its workspace since it started or was last told.
It is read-only until there is news, answers each handoff once, and says in its text that a handoff is information."""

from app.database import get_db
from app.services import activity_service, agent_service, workspace_service

SCOPE = "workspace:news-test"


def _setup():
    with get_db() as conn:
        owner = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
    workspace_service.create_workspace("news-test", "News Test", owner)
    workspace_service.create_workspace("news-other", "News Other", owner)
    heads = {}
    for name in ("news-a", "news-b"):
        _, issued = agent_service.create_agent(
            agent_id=name, display_name=name, owner_user_id=owner,
            read_scopes=[SCOPE, "workspace:news-other"], write_scopes=[SCOPE, "workspace:news-other"],
        )
        heads[name] = {"Authorization": f"Bearer {issued}"}
    return heads, owner


def _start(client, headers, ref, scope=SCOPE):
    response = client.post("/api/session-context", headers=headers, json={"memory_scope": scope, "host_session_ref": ref})
    assert response.status_code == 200, response.json()
    return response.json()["data"]["execution_id"]


def _news(client, headers, ref):
    response = client.post("/api/session-context/new-handoffs", headers=headers, json={"host_session_ref": ref})
    assert response.status_code == 200, response.json()
    return response.json()["data"]


def _close(agent, owner, execution, task, result="Done.", steps=("Review it.",), asks=None, scope=SCOPE):
    activity = activity_service.create_activity(
        agent_id=agent, user_id=owner, task_description=task, memory_scope=scope, source_execution_id=execution,
    )
    activity_service.update_activity(activity["id"], status="completed", task_result=result)
    handoff = {"completed": result, "next_steps": list(steps)}
    if asks:
        handoff["needs_human"] = asks
    activity_service.set_handoff(activity["id"], handoff)
    return activity["id"]


def _marks():
    with get_db() as conn:
        return conn.execute("SELECT COUNT(*) FROM execution_handoff_marks").fetchone()[0]


def test_nothing_new_costs_no_write(test_client, admin_token):
    heads, owner = _setup()
    _start(test_client, heads["news-a"], "a-1")
    assert _news(test_client, heads["news-a"], "a-1") == {"count": 0, "items": [], "text": ""}
    assert _news(test_client, heads["news-a"], "never-started") == {"count": 0, "items": [], "text": ""}
    assert _marks() == 0


def test_a_handoff_closed_before_the_session_started_is_not_news(test_client, admin_token):
    heads, owner = _setup()
    b = _start(test_client, heads["news-b"], "b-1")
    _close("news-b", owner, b, "Old work")
    _start(test_client, heads["news-a"], "a-1")  # the session context already carries it
    assert _news(test_client, heads["news-a"], "a-1")["count"] == 0


def test_another_sessions_handoff_arrives_once_and_says_it_is_not_an_approval(test_client, admin_token):
    heads, owner = _setup()
    a = _start(test_client, heads["news-a"], "a-1")
    b = _start(test_client, heads["news-b"], "b-1")
    activity_id = _close("news-b", owner, b, "Migrate the importer", result="Importer migrated.", steps=("Run the checks", "Tell the owner"),
                         asks="Approve the deploy")
    news = _news(test_client, heads["news-a"], "a-1")
    assert news["count"] == 1 and news["more"] is False
    item = news["items"][0]
    assert item["id"] == activity_id and item["agent_id"] == "news-b" and item["session"] == b
    assert item["task"] == "Migrate the importer" and item["result"] == "Importer migrated."
    assert item["next_steps"] == ["Run the checks", "Tell the owner"] and item["needs_human"] == "Approve the deploy"
    text = news["text"]
    assert activity_id in text and "Importer migrated." in text and "Approve the deploy" in text
    assert "not a person's approval of anything" in text and "approval-gated" in text
    assert a != b
    # delivered once: the same call again is silent, and the mark is stored
    assert _news(test_client, heads["news-a"], "a-1")["count"] == 0 and _marks() == 1
    # a handoff closed later arrives on its own
    second = _close("news-b", owner, b, "Second task")
    assert [i["id"] for i in _news(test_client, heads["news-a"], "a-1")["items"]] == [second]
    with get_db() as conn:
        delivered = conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'handoff_news_delivered'").fetchone()[0]
    assert delivered == 2


def test_a_sessions_own_work_is_left_out(test_client, admin_token):
    heads, owner = _setup()
    a = _start(test_client, heads["news-a"], "a-1")
    _close("news-a", owner, a, "My own task")
    # the same agent, with no execution recorded, cannot be told apart from this session either
    bare = activity_service.create_activity(agent_id="news-a", user_id=owner, task_description="No execution", memory_scope=SCOPE)
    activity_service.update_activity(bare["id"], status="completed", task_result="Done.")
    activity_service.set_handoff(bare["id"], {"completed": "Done."})
    assert _news(test_client, heads["news-a"], "a-1")["count"] == 0
    # another session of the same agent that did pass its execution is news
    a2 = _start(test_client, heads["news-a"], "a-2")
    other = _close("news-a", owner, a2, "From my other session")
    assert [i["id"] for i in _news(test_client, heads["news-a"], "a-1")["items"]] == [other]


def test_only_closed_work_with_a_handoff_in_this_workspace_counts(test_client, admin_token):
    heads, owner = _setup()
    _start(test_client, heads["news-a"], "a-1")
    b = _start(test_client, heads["news-b"], "b-1")
    b_other = _start(test_client, heads["news-b"], "b-2", scope="workspace:news-other")
    activity_service.create_activity(agent_id="news-b", user_id=owner, task_description="Still open", memory_scope=SCOPE, source_execution_id=b)
    closed_without = activity_service.create_activity(agent_id="news-b", user_id=owner, task_description="No handoff", memory_scope=SCOPE, source_execution_id=b)
    activity_service.update_activity(closed_without["id"], status="completed", task_result="Done.")
    _close("news-b", owner, b_other, "Elsewhere", scope="workspace:news-other")
    assert _news(test_client, heads["news-a"], "a-1")["count"] == 0


def test_a_burst_is_delivered_in_pages(test_client, admin_token):
    heads, owner = _setup()
    _start(test_client, heads["news-a"], "a-1")
    b = _start(test_client, heads["news-b"], "b-1")
    ids = [_close("news-b", owner, b, f"Task {n}") for n in range(7)]
    first = _news(test_client, heads["news-a"], "a-1")
    assert first["count"] == 5 and first["more"] is True and "More are waiting" in first["text"]
    assert [i["id"] for i in first["items"]] == ids[:5]
    second = _news(test_client, heads["news-a"], "a-1")
    assert [i["id"] for i in second["items"]] == ids[5:] and second["more"] is False
    assert _news(test_client, heads["news-a"], "a-1")["count"] == 0


def test_a_scope_the_caller_can_no_longer_read_gives_no_news(test_client, admin_token):
    from app.services import session_context_service

    heads, owner = _setup()
    _start(test_client, heads["news-a"], "a-1")
    b = _start(test_client, heads["news-b"], "b-1")
    _close("news-b", owner, b, "Hidden")
    hidden = session_context_service.new_handoffs(agent_id="news-a", user_id=owner, host_session_ref="a-1", can_read=lambda scope: False)
    assert hidden["count"] == 0 and _marks() == 0
    assert _news(test_client, heads["news-a"], "a-1")["count"] == 1


def test_the_route_needs_a_host_session_and_an_agent(test_client, admin_token):
    heads, _ = _setup()
    assert test_client.post("/api/session-context/new-handoffs", headers=heads["news-a"], json={}).status_code == 422
    assert test_client.post("/api/session-context/new-handoffs", json={"host_session_ref": "x"}).status_code in (401, 403)


def test_an_existing_database_gets_the_marks_table_from_a_migration():
    import sqlite3

    from app.schema import create_schema

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    create_schema(conn)
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'execution_handoff_marks'").fetchone()
    # a database as it was at revision 31: no table, revision 32 not recorded
    conn.execute("DROP TABLE execution_handoff_marks")
    conn.execute("DELETE FROM schema_migrations WHERE revision = 32")
    conn.commit()
    create_schema(conn)
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'execution_handoff_marks'").fetchone()
    assert conn.execute("SELECT name FROM schema_migrations WHERE revision = 32").fetchone()["name"] == "execution-handoff-marks"
