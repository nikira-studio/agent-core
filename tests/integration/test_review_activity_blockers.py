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


def test_without_an_execution_id_a_different_task_never_ends_the_agents_older_record(test_client, admin_token):
    key = _setup()
    a = _ok(test_client, key, "activity_update", memory_scope=WS, task_description="Migrate the billing database to the new schema", status="active")["activity"]
    b = _ok(test_client, key, "activity_update", memory_scope=WS, task_description="Write the quarterly newsletter draft", status="active")
    assert b["activity"]["id"] != a["id"]
    assert {w["code"] for w in b["warnings"]} == {"OTHER_OPEN_TASK_KEPT"}
    assert activity_service.get_activity(a["id"])["status"] == "active"
    assert activity_service.get_activity(b["activity"]["id"])["status"] == "active"


def test_two_sessions_of_one_agent_without_ids_interleave_and_keep_both_records(test_client, admin_token):
    key = _setup()
    one = "Migrate the billing database to the new schema"
    two = "Write the quarterly newsletter draft"
    a = _ok(test_client, key, "activity_update", memory_scope=WS, task_description=one, status="active")["activity"]["id"]
    b = _ok(test_client, key, "activity_update", memory_scope=WS, task_description=two, status="active")["activity"]["id"]
    # each session keeps announcing its own task: it continues its own record, no third record appears
    again_a = _ok(test_client, key, "activity_update", memory_scope=WS, task_description=one, task_note="step 2 done")
    again_b = _ok(test_client, key, "activity_update", memory_scope=WS, task_description=two, task_note="draft written")
    assert again_a["activity"]["id"] == a and again_b["activity"]["id"] == b
    assert "warnings" not in again_a and "warnings" not in again_b
    with get_db() as conn:
        rows = conn.execute("SELECT id, status FROM agent_activity WHERE assigned_agent_id='worker'").fetchall()
    assert sorted(r["status"] for r in rows) == ["active", "active"] and {r["id"] for r in rows} == {a, b}
    assert "cancelled" not in [activity_service.get_activity(i)["status"] for i in (a, b)]


def test_with_execution_ids_two_sessions_keep_their_own_records_as_before(test_client, admin_token):
    key = _setup()
    s1, s2 = _execution(test_client, key, "s1"), _execution(test_client, key, "s2")
    a = _ok(test_client, key, "activity_update", memory_scope=WS, execution_id=s1, task_description="Migrate the billing database to the new schema", status="active")["activity"]["id"]
    b = _ok(test_client, key, "activity_update", memory_scope=WS, execution_id=s2, task_description="Write the quarterly newsletter draft", status="active")["activity"]["id"]
    assert a != b and activity_service.get_activity(a)["status"] == "active" and activity_service.get_activity(b)["status"] == "active"


# ---- a wrong memory_scope is fixable in one retry and leaves a trace ------------------------------------------------

def _agent(key_id, write_scopes, read_scopes=None):
    with get_db() as conn:
        owner = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
    _, key = agent_service.create_agent(
        agent_id=key_id, display_name=key_id, owner_user_id=owner,
        read_scopes=read_scopes or write_scopes, write_scopes=write_scopes,
    )
    return key


def _drop_private_scope(agent_id):
    import json

    with get_db() as conn:
        for column in ("read_scopes_json", "write_scopes_json"):
            current = json.loads(conn.execute(f"SELECT {column} FROM agents WHERE id=?", (agent_id,)).fetchone()[0])
            conn.execute(f"UPDATE agents SET {column}=? WHERE id=?", (json.dumps([x for x in current if x != f"agent:{agent_id}"]), agent_id))


def _denied_events(agent_id):
    with get_db() as conn:
        return conn.execute(
            "SELECT details_json, result FROM audit_log WHERE action='scope_denied' AND actor_id=? ORDER BY id", (agent_id,)
        ).fetchall()


def test_a_refused_memory_scope_names_the_scopes_the_caller_may_use_and_is_audited(test_client, admin_token):
    key = _agent("advisor-x", ["agent:advisor-x"])
    r = _mcp(test_client, key, "activity_update", memory_scope="workspace:finance", task_description="Month end", status="active")
    assert r.status_code == 403
    error = r.json()["error"]
    assert error["code"] == "SCOPE_DENIED"
    assert "'workspace:finance'" in error["message"] and "agent:advisor-x" in error["message"]
    assert "leave memory_scope out" in error["message"]
    rows = _denied_events("advisor-x")
    assert len(rows) == 1 and rows[0]["result"] == "blocked"
    details = rows[0]["details_json"]
    assert "workspace:finance" in details and "activity_update" in details


def test_a_long_list_of_scopes_in_a_refusal_is_cut_short(test_client, admin_token):
    with get_db() as conn:
        owner = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
    for i in range(15):
        workspace_service.create_workspace(f"many-{i}", f"many-{i}", owner)
    key = _agent("busy", ["agent:busy"] + [f"workspace:many-{i}" for i in range(15)])
    r = _mcp(test_client, key, "activity_update", memory_scope="workspace:nope", task_description="x task", status="active")
    message = r.json()["error"]["message"]
    assert "more (effective_authority lists them all)" in message and message.count("workspace:many-") <= 12


def test_an_omitted_scope_uses_the_private_scope_and_never_guesses_among_several(test_client, admin_token):
    key = _agent("solo", ["agent:solo"])
    out = _ok(test_client, key, "activity_update", task_description="Own private task", status="active")
    assert out["activity"]["memory_scope"] == "agent:solo"
    # no private scope but exactly one writable scope: that one
    with get_db() as conn:
        owner = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
    workspace_service.create_workspace("only-ws", "only-ws", owner)
    workspace_service.create_workspace("other-ws", "other-ws", owner)
    one = _agent("one-scope", ["workspace:only-ws"])
    _drop_private_scope("one-scope")
    got = _ok(test_client, one, "activity_update", task_description="Only workspace task", status="active")
    assert got["activity"]["memory_scope"] == "workspace:only-ws"
    # several writable scopes and no private one: no guess, the refusal lists them
    two = _agent("two-scopes", ["workspace:only-ws", "workspace:other-ws"])
    _drop_private_scope("two-scopes")
    r = _mcp(test_client, two, "activity_update", task_description="Which one?", status="active")
    assert r.status_code == 403 and "workspace:only-ws" in r.json()["error"]["message"] and "workspace:other-ws" in r.json()["error"]["message"]


def test_a_wrong_execution_id_is_ignored_with_advice_not_refused(test_client, admin_token):
    key = _setup()
    out = _ok(test_client, key, "activity_update", memory_scope=WS, execution_id="not-a-real-id", task_description="Keep my write", status="active")
    warning = out["warnings"][0]
    assert warning["code"] == "EXECUTION_ID_IGNORED"
    assert "Leave execution_id out, or use the one session_context returns" in warning["message"]
    assert out["activity"]["status"] == "active"
    other = _execution(test_client, key, "elsewhere")
    mismatch = _ok(test_client, key, "activity_update", memory_scope="agent:worker", execution_id=other, task_description="Wrong scope id", status="active")
    assert "session_context returned for this scope" in mismatch["warnings"][0]["message"]


def test_the_tool_schemas_describe_memory_scope_in_plain_words(test_client, admin_token):
    key = _setup()
    listed = test_client.post("/mcp", headers={"Authorization": f"Bearer {key}"}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()
    tools = {t["name"]: t for t in listed["result"]["tools"]}
    for name in ("activity_update", "session_context", "workspace_sync", "workspace_sync_ack"):
        text = tools[name]["inputSchema"]["properties"]["memory_scope"]["description"]
        assert "agent:<your own agent id>" in text and "workspace:<name>" in text, name
    assert "Leave it out" in tools["activity_update"]["inputSchema"]["properties"]["memory_scope"]["description"]
    assert "Leave it out if you have none" in tools["activity_update"]["inputSchema"]["properties"]["execution_id"]["description"]
