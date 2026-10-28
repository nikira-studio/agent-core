"""The "up" path: coordinators read what is waiting on a person.

A coordinator (an attention reader, a chief-of-staff agent) is an ordinary,
non-admin agent. It sees needs_human handoffs in the scopes it reads, opens the
activities behind them, and hears about handoffs through webhooks and the
workspace change feed.
"""

import json

from app.database import get_db
from app.services import activity_service, agent_service, webhook_service, workspace_service

A = "workspace:att-a"
B = "workspace:att-b"
HIDDEN = "workspace:att-hidden"


def _setup():
    with get_db() as conn:
        owner_id = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
    for ws in ("att-a", "att-b", "att-hidden"):
        workspace_service.create_workspace(ws, ws, owner_id)
    _, worker_key = agent_service.create_agent(
        agent_id="att-worker", display_name="Worker", owner_user_id=owner_id,
        read_scopes=[A, B, HIDDEN], write_scopes=[A, B, HIDDEN],
    )
    _, coordinator_key = agent_service.create_agent(
        agent_id="att-coordinator", display_name="Coordinator", owner_user_id=owner_id,
        read_scopes=[A, B], write_scopes=[],
    )
    return (
        {"Authorization": f"Bearer {worker_key}"},
        {"Authorization": f"Bearer {coordinator_key}"},
    )


def _mcp(client, headers, tool, **params):
    return client.post("/mcp", headers=headers, json={"tool": tool, "params": params})


def _waiting(scope, needs_human, task_ref=None):
    activity = activity_service.create_activity(
        agent_id="att-worker", user_id="u", task_description=f"Work in {scope}",
        memory_scope=scope,
        metadata_json=activity_service.with_task_ref(None, task_ref) if task_ref else None,
    )
    activity_service.update_activity(activity["id"], status="blocked")
    activity_service.set_handoff(activity["id"], {"needs_human": needs_human})
    return activity


def test_coordinator_lists_needs_human_across_readable_scopes_only(test_client, admin_token):
    _, coordinator = _setup()
    in_a = _waiting(A, "Approve the A release")
    in_b = _waiting(B, "Pick a B vendor", task_ref="openagents:42")
    _waiting(HIDDEN, "Not for the coordinator")

    response = _mcp(test_client, coordinator, "attention_list")
    assert response.status_code == 200, response.json()
    items = response.json()["data"]["items"]
    assert {i["id"] for i in items} == {in_a["id"], in_b["id"]}
    by_id = {i["id"]: i for i in items}
    assert by_id[in_b["id"]]["task_ref"] == "openagents:42"
    assert by_id[in_a["id"]]["needs_human"] == "Approve the A release"

    # A queue that already lists openagents tasks skips the ones pointing there.
    untracked = _mcp(test_client, coordinator, "attention_list", without_task_ref=True).json()["data"]
    assert [i["id"] for i in untracked["items"]] == [in_a["id"]]

    narrowed = _mcp(test_client, coordinator, "attention_list", scopes=[B]).json()["data"]
    assert [i["id"] for i in narrowed["items"]] == [in_b["id"]]
    denied = _mcp(test_client, coordinator, "attention_list", scopes=[HIDDEN])
    assert denied.status_code == 403

    rest = test_client.get("/api/activity/attention", headers=coordinator)
    assert rest.status_code == 200, rest.json()
    assert {i["id"] for i in rest.json()["data"]["items"]} == {in_a["id"], in_b["id"]}
    rest_narrowed = test_client.get(
        "/api/activity/attention", headers=coordinator,
        params={"scope": [A], "without_task_ref": "true"},
    ).json()["data"]
    assert [i["id"] for i in rest_narrowed["items"]] == [in_a["id"]]
    assert test_client.get(
        "/api/activity/attention", headers=coordinator, params={"scope": [HIDDEN]}
    ).status_code == 403

    # Resolved requests drop off.
    activity_service.resolve_handoff(in_a["id"])
    after = _mcp(test_client, coordinator, "attention_list").json()["data"]
    assert [i["id"] for i in after["items"]] == [in_b["id"]]


def test_activity_get_opens_what_activity_list_shows(test_client, admin_token):
    _, coordinator = _setup()
    readable = _waiting(A, "Decide")
    hidden = _waiting(HIDDEN, "Decide")

    listed = _mcp(test_client, coordinator, "activity_list").json()["data"]["activities"]
    assert readable["id"] in {a["id"] for a in listed}
    assert _mcp(test_client, coordinator, "activity_get", activity_id=readable["id"]).status_code == 200
    assert _mcp(test_client, coordinator, "activity_get", activity_id=hidden["id"]).status_code == 403

    assert test_client.get(f"/api/activity/{readable['id']}", headers=coordinator).status_code == 200
    assert test_client.get(f"/api/activity/{hidden['id']}", headers=coordinator).status_code == 403
    # Reading is not updating.
    assert test_client.put(
        f"/api/activity/{readable['id']}", headers=coordinator, json={"task_note": "x"}
    ).status_code == 403


def test_task_ref_is_stored_and_validated(test_client, admin_token):
    worker, _ = _setup()
    created = _mcp(test_client, worker, "activity_update", memory_scope=A,
                   task_description="Run task 7", task_ref="openagents:7")
    assert created.status_code == 201, created.json()
    activity = created.json()["data"]["activity"]
    assert json.loads(activity["metadata_json"]) == {"task_ref": "openagents:7"}

    moved = _mcp(test_client, worker, "activity_update", memory_scope=A, task_ref="foundry:gantt")
    assert moved.status_code == 200, moved.json()
    assert json.loads(moved.json()["data"]["activity"]["metadata_json"])["task_ref"] == "foundry:gantt"

    bad = _mcp(test_client, worker, "activity_update", memory_scope=A, task_ref="no system prefix")
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "INVALID_TASK_REF"

    rest = test_client.post("/api/activity", headers=worker, json={
        "task_description": "From REST", "memory_scope": B,
        "metadata_json": json.dumps({"origin": "hermes"}), "task_ref": "openagents:9",
    })
    assert rest.status_code == 201, rest.json()
    assert json.loads(rest.json()["data"]["activity"]["metadata_json"]) == {
        "origin": "hermes", "task_ref": "openagents:9",
    }
    rest_bad = test_client.post("/api/activity", headers=worker, json={
        "task_description": "Bad", "memory_scope": B,
        "metadata_json": json.dumps({"task_ref": "openagents:"}),
    })
    assert rest_bad.status_code == 400


def test_handoffs_reach_webhooks_and_the_change_feed(test_client, admin_token, monkeypatch):
    worker, _ = _setup()
    events = []
    monkeypatch.setattr(webhook_service, "dispatch_event", lambda event, payload: events.append((event, payload)))

    created = test_client.post("/api/activity", headers=worker, json={
        "task_description": "Draft the release", "memory_scope": A, "task_ref": "foundry:release-1",
    }).json()["data"]["activity"]
    # A handoff alone is a valid update; it used to fail with UPDATE_FAILED.
    closed = test_client.put(f"/api/activity/{created['id']}", headers=worker, json={
        "status": "blocked",
        "handoff": {"completed": "Drafted", "needs_human": "Approve the release"},
    })
    assert closed.status_code == 200, closed.json()
    only_handoff = test_client.put(f"/api/activity/{created['id']}", headers=worker, json={
        "handoff": {"completed": "Drafted", "needs_human": "Approve the release"},
    })
    assert only_handoff.status_code == 200, only_handoff.json()

    event, payload = events[-1]
    assert event == "activity_updated"
    assert payload["status"] == "blocked"
    assert payload["task_ref"] == "foundry:release-1"
    assert payload["handoff"]["needs_human"] == "Approve the release"
    assert payload["handoff_resolved_at"] is None

    resolved = test_client.post(f"/api/activity/{created['id']}/handoff/resolve", cookies=_admin_cookie())
    assert resolved.status_code == 200, resolved.json()
    event, payload = events[-1]
    assert event == "activity_handoff_resolved"
    assert payload["handoff_resolved_at"]

    with get_db() as conn:
        changes = conn.execute(
            "SELECT change_type, summary_json FROM workspace_changes "
            "WHERE memory_scope = ? AND resource_id = ? ORDER BY sequence",
            (A, created["id"]),
        ).fetchall()
    types = [c["change_type"] for c in changes]
    assert "handoff_recorded" in types and types[-1] == "handoff_resolved"
    recorded = json.loads(next(c["summary_json"] for c in changes if c["change_type"] == "handoff_recorded"))
    assert recorded["needs_human"] == "Approve the release"
    assert recorded["task_ref"] == "foundry:release-1"


def _admin_cookie():
    from app.services.auth_service import create_session

    with get_db() as conn:
        admin_id = conn.execute("SELECT id FROM users WHERE role = 'admin' LIMIT 1").fetchone()["id"]
    return {"session_token": create_session(admin_id, channel="dashboard")["session_id"]}


def test_webhook_offers_the_handoff_resolved_event():
    assert "activity_handoff_resolved" in webhook_service.WEBHOOK_EVENT_TYPES
    sample = webhook_service._sample_payload("activity_handoff_resolved")
    assert sample["handoff"]["needs_human"] and sample["handoff_resolved_at"]
