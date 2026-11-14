"""An execution_id that is stale, another agent's or for another scope must not lose a write.

The write is saved without that attribution and the response carries an EXECUTION_ID_IGNORED warning. Where the id is
the whole target of a call (workspace_sync), the hard error stays.
"""

from app.database import get_db
from app.services import agent_service, workspace_service

WS = "workspace:exec-a"


def _mcp(client, key, tool, _ok=(200, 201), **params):
    r = client.post("/mcp", headers={"Authorization": f"Bearer {key}"}, json={"tool": tool, "params": params})
    assert r.status_code in _ok, (tool, r.status_code, r.text)
    return r.json()


def _setup():
    with get_db() as conn:
        owner = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
    workspace_service.create_workspace("exec-a", "exec-a", owner)
    keys = {}
    for name in ("alpha", "beta"):
        _, keys[name] = agent_service.create_agent(
            agent_id=name, display_name=name, owner_user_id=owner,
            read_scopes=[WS, f"agent:{name}"], write_scopes=[WS, f"agent:{name}"],
        )
    return keys


def _ignored(payload):
    return [w for w in (payload["data"].get("warnings") or []) if w.get("code") == "EXECUTION_ID_IGNORED"]


def _source(table, record_id):
    with get_db() as conn:
        return conn.execute(f"SELECT source_execution_id AS s FROM {table} WHERE id = ?", (record_id,)).fetchone()["s"]


def test_an_unknown_execution_id_still_saves_the_activity_and_the_memory(test_client, admin_token):
    keys = _setup()
    out = _mcp(test_client, keys["alpha"], "activity_update", memory_scope=WS, task_description="Weekly report",
               status="active", execution_id="remembered-from-before-a-restart")
    assert out["ok"] and out["data"]["activity"]["task_description"] == "Weekly report"
    warning = _ignored(out)
    assert len(warning) == 1 and warning[0]["reason"] == "EXECUTION_NOT_FOUND"
    assert "saved without it" in warning[0]["message"]
    assert _source("agent_activity", out["data"]["activity"]["id"]) is None

    note = _mcp(test_client, keys["alpha"], "activity_update", memory_scope=WS, task_note="halfway",
                execution_id="remembered-from-before-a-restart")
    assert note["ok"] and _ignored(note)

    mem = _mcp(test_client, keys["alpha"], "memory_write", content="The weekly report goes out on Fridays at noon.",
               memory_class="fact", scope=WS, topic="weekly report", execution_id="remembered-from-before-a-restart")
    assert mem["ok"] and _ignored(mem)
    assert _source("memory_records", mem["data"]["record"]["id"]) is None


def test_another_agents_or_another_scopes_execution_is_never_attributed(test_client, admin_token):
    keys = _setup()
    ctx = _mcp(test_client, keys["alpha"], "session_context", memory_scope=WS, host_session_ref="a1")["data"]
    alpha_execution = ctx["execution_id"]

    # beta passes alpha's execution: saved, not attributed to alpha's session
    stolen = _mcp(test_client, keys["beta"], "memory_write", content="Beta noted the invoices close on the 5th.",
                  memory_class="fact", scope=WS, topic="invoices", execution_id=alpha_execution)
    assert _ignored(stolen)[0]["reason"] == "EXECUTION_OWNERSHIP"
    assert _source("memory_records", stolen["data"]["record"]["id"]) is None

    # alpha's own execution, but a write to another scope than it was started for
    other = _mcp(test_client, keys["alpha"], "memory_write", content="Alpha private scratch about the report layout.",
                 memory_class="scratchpad", scope="agent:alpha", topic="scratch", execution_id=alpha_execution)
    assert _ignored(other)[0]["reason"] == "EXECUTION_SCOPE_MISMATCH"
    assert _source("memory_records", other["data"]["record"]["id"]) is None


def test_a_valid_execution_is_still_attributed_without_a_warning(test_client, admin_token):
    keys = _setup()
    execution = _mcp(test_client, keys["alpha"], "session_context", memory_scope=WS, host_session_ref="a2")["data"]["execution_id"]
    out = _mcp(test_client, keys["alpha"], "activity_update", memory_scope=WS, task_description="Real task",
               status="active", execution_id=execution)
    assert not _ignored(out)
    assert _source("agent_activity", out["data"]["activity"]["id"]) == execution
    mem = _mcp(test_client, keys["alpha"], "memory_write", content="Alpha confirmed the report template lives in the shared drive.",
               memory_class="fact", scope=WS, topic="template", execution_id=execution)
    assert not _ignored(mem) and _source("memory_records", mem["data"]["record"]["id"]) == execution


def test_the_rest_routes_follow_the_same_rule(test_client, admin_token):
    keys = _setup()
    headers = {"Authorization": f"Bearer {keys['alpha']}"}
    made = test_client.post("/api/activity", headers=headers, json={
        "task_description": "REST task", "memory_scope": WS, "execution_id": "stale"})
    assert made.status_code == 201, made.text
    assert made.json()["data"]["warnings"][0]["code"] == "EXECUTION_ID_IGNORED"
    activity_id = made.json()["data"]["activity"]["id"]
    assert _source("agent_activity", activity_id) is None

    updated = test_client.put(f"/api/activity/{activity_id}", headers=headers, json={"task_note": "n", "execution_id": "stale"})
    assert updated.status_code == 200, updated.text
    assert updated.json()["data"]["warnings"][0]["code"] == "EXECUTION_ID_IGNORED"

    mem = test_client.post("/api/memory/write", headers=headers, json={
        "content": "REST wrote the quarterly figures to the shared sheet.", "memory_class": "fact", "scope": WS,
        "topic": "figures", "execution_id": "stale"})
    assert mem.status_code in (200, 201), mem.text
    assert any(w["code"] == "EXECUTION_ID_IGNORED" for w in mem.json()["data"]["warnings"])


def test_where_the_id_is_the_whole_target_the_hard_error_stays(test_client, admin_token):
    keys = _setup()
    sync = _mcp(test_client, keys["alpha"], "workspace_sync", _ok=(400,), memory_scope=WS, execution_id="stale")
    assert not sync["ok"] and "not known here" in sync["error"]["message"]
