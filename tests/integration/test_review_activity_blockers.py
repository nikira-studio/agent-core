"""Review blocker B2: opening and closing a task in one call keeps everything; a different task is never a rename."""

from app.database import get_db
from app.services import activity_service, agent_service, workspace_service

WS = "workspace:rb-a"


def _mcp(client, key, tool, **params):
    r = client.post("/mcp", headers={"Authorization": f"Bearer {key}"}, json={"tool": tool, "params": params})
    return r


def _ok(client, key, tool, **params):
    r = _mcp(client, key, tool, **params)
    assert r.status_code in (200, 201), (tool, r.text)
    return r.json()["data"]


def _setup():
    with get_db() as conn:
        owner = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
    workspace_service.create_workspace("rb-a", "rb-a", owner)
    _, key = agent_service.create_agent(agent_id="worker", display_name="worker", owner_user_id=owner, read_scopes=[WS, "agent:worker"], write_scopes=[WS, "agent:worker"])
    return key


def _execution(client, key, ref="s1"):
    return _ok(client, key, "session_context", memory_scope=WS, host_session_ref=ref)["execution_id"]


def test_open_and_close_in_one_call_stores_the_result_and_the_ask(test_client, admin_token):
    key = _setup()
    ex = _execution(test_client, key)
    out = _ok(test_client, key, "activity_update", memory_scope=WS, execution_id=ex, task_description="Review the export format",
              status="blocked", task_result="Needs a decision before I can continue.", handoff={"needs_human": "Pick the delimiter for exports."})
    stored = activity_service.get_activity(out["activity"]["id"])
    assert stored["status"] == "blocked" and stored["task_result"] == "Needs a decision before I can continue."
    assert "Pick the delimiter" in stored["handoff_json"]
    items = _ok(test_client, key, "attention_list")["items"]
    assert [i["id"] for i in items] == [stored["id"]]
    # and a completed one
    done = _ok(test_client, key, "activity_update", memory_scope=WS, execution_id=ex, task_description="Rename the helper",
               status="completed", task_result="Renamed in app/util.py and updated the callers.", handoff={"completed": "Rename done"})
    row = activity_service.get_activity(done["activity"]["id"])
    assert row["status"] == "completed" and row["task_result"].startswith("Renamed") and "Rename done" in row["handoff_json"]


def test_completing_a_new_task_without_a_result_is_refused_and_creates_nothing(test_client, admin_token):
    key = _setup()
    ex = _execution(test_client, key)
    r = _mcp(test_client, key, "activity_update", memory_scope=WS, execution_id=ex, task_description="Quick fix", status="completed")
    assert r.status_code == 400 and r.json()["error"]["code"] == "TASK_RESULT_REQUIRED"
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM agent_activity WHERE task_description = 'Quick fix'").fetchone()[0] == 0


def test_a_different_description_while_a_task_is_open_does_not_rename_it(test_client, admin_token):
    key = _setup()
    ex = _execution(test_client, key)
    first = _ok(test_client, key, "activity_update", memory_scope=WS, execution_id=ex, task_description="Migrate the billing database to the new schema", status="active")["activity"]
    second = _ok(test_client, key, "activity_update", memory_scope=WS, execution_id=ex, task_description="Write the quarterly newsletter draft", status="active")
    assert second["activity"]["id"] != first["id"]
    assert {w["code"] for w in second["warnings"]} == {"PREVIOUS_TASK_ENDED"}
    old = activity_service.get_activity(first["id"])
    assert old["task_description"] == "Migrate the billing database to the new schema" and old["status"] == "cancelled"
    assert old["task_result"].startswith("Ended without report")
    # A refinement of the same task is an edit, not a new task.
    refined = _ok(test_client, key, "activity_update", memory_scope=WS, execution_id=ex, task_description="Write the quarterly newsletter draft for the March issue", status="active")
    assert refined["activity"]["id"] == second["activity"]["id"] and "warnings" not in refined


def test_a_new_task_that_is_closed_in_the_same_call_as_it_replaces_another(test_client, admin_token):
    key = _setup()
    ex = _execution(test_client, key)
    first = _ok(test_client, key, "activity_update", memory_scope=WS, execution_id=ex, task_description="Investigate the failing nightly import", status="active")["activity"]
    out = _ok(test_client, key, "activity_update", memory_scope=WS, execution_id=ex, task_description="Update the onboarding checklist wording",
              status="completed", task_result="Reworded four steps in docs/onboarding.md.")
    assert out["activity"]["status"] == "completed" and out["activity"]["id"] != first["id"]
    assert activity_service.get_activity(first["id"])["status"] == "cancelled"
