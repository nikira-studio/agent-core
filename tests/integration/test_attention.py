"""The "up" path: coordinators read what is waiting on a person.

A coordinator (an attention reader, a chief-of-staff agent) is an ordinary,
non-admin agent. It sees needs_human handoffs in the scopes it reads, opens the
activities behind them, and hears about handoffs through webhooks and the
workspace change feed.
"""

import json

from app.database import get_db
from app.services import activity_service, agent_service, webhook_service, workspace_service
from tests.conftest import csrf_headers

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
    in_b = _waiting(B, "Pick a B vendor", task_ref="tracker:42")
    _waiting(HIDDEN, "Not for the coordinator")

    response = _mcp(test_client, coordinator, "attention_list")
    assert response.status_code == 200, response.json()
    items = response.json()["data"]["items"]
    assert {i["id"] for i in items} == {in_a["id"], in_b["id"]}
    by_id = {i["id"]: i for i in items}
    assert by_id[in_b["id"]]["task_ref"] == "tracker:42"
    assert by_id[in_a["id"]]["needs_human"] == "Approve the A release"

    # A queue that already lists tracker tasks skips the ones pointing there.
    untracked = _mcp(test_client, coordinator, "attention_list", without_task_ref=True).json()["data"]
    assert [i["id"] for i in untracked["items"]] == [in_a["id"]]

    narrowed = _mcp(test_client, coordinator, "attention_list", scopes=[B]).json()["data"]
    assert [i["id"] for i in narrowed["items"]] == [in_b["id"]]
    denied = _mcp(test_client, coordinator, "attention_list", scopes=[HIDDEN])
    assert denied.status_code == 403

    rest = test_client.get("/api/activity/attention", headers=coordinator)
    assert rest.status_code == 200, rest.json()
    assert {i["id"] for i in rest.json()["data"]["items"]} == {in_a["id"], in_b["id"]}
    rest_untracked = test_client.get(
        "/api/activity/attention", headers=coordinator, params={"without_task_ref": "true"},
    ).json()["data"]
    assert [i["id"] for i in rest_untracked["items"]] == [in_a["id"]]
    rest_narrowed = test_client.get(
        "/api/activity/attention", headers=coordinator, params={"scope": [B]},
    ).json()["data"]
    assert [i["id"] for i in rest_narrowed["items"]] == [in_b["id"]]
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
                   task_description="Run task 7", task_ref="tracker:7")
    assert created.status_code == 201, created.json()
    activity = created.json()["data"]["activity"]
    assert json.loads(activity["metadata_json"]) == {"task_ref": "tracker:7"}

    moved = _mcp(test_client, worker, "activity_update", memory_scope=A, task_ref="jobs:gantt")
    assert moved.status_code == 200, moved.json()
    assert json.loads(moved.json()["data"]["activity"]["metadata_json"])["task_ref"] == "jobs:gantt"

    bad = _mcp(test_client, worker, "activity_update", memory_scope=A, task_ref="no system prefix")
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "INVALID_TASK_REF"

    rest = test_client.post("/api/activity", headers=worker, json={
        "task_description": "From REST", "memory_scope": B,
        "metadata_json": json.dumps({"origin": "hermes"}), "task_ref": "tracker:9",
    })
    assert rest.status_code == 201, rest.json()
    assert json.loads(rest.json()["data"]["activity"]["metadata_json"]) == {
        "origin": "hermes", "task_ref": "tracker:9",
    }
    rest_bad = test_client.post("/api/activity", headers=worker, json={
        "task_description": "Bad", "memory_scope": B,
        "metadata_json": json.dumps({"task_ref": "tracker:"}),
    })
    assert rest_bad.status_code == 400


def test_handoffs_reach_webhooks_and_the_change_feed(test_client, admin_token, monkeypatch):
    worker, _ = _setup()
    events = []
    monkeypatch.setattr(webhook_service, "dispatch_event", lambda event, payload: events.append((event, payload)))

    created = test_client.post("/api/activity", headers=worker, json={
        "task_description": "Draft the release", "memory_scope": A, "task_ref": "jobs:release-1",
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
    assert payload["task_ref"] == "jobs:release-1"
    assert payload["handoff"]["needs_human"] == "Approve the release"
    assert payload["handoff_resolved_at"] is None

    # A person resolves it, with the browser's session cookie and its CSRF token.
    test_client.cookies.clear()
    test_client.cookies.set("session_token", _admin_session())
    resolved = test_client.post(
        f"/api/activity/{created['id']}/handoff/resolve",
        headers=csrf_headers(test_client),
    )
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
    assert recorded["task_ref"] == "jobs:release-1"


def _admin_session():
    from app.services.auth_service import create_session

    with get_db() as conn:
        admin_id = conn.execute("SELECT id FROM users WHERE role = 'admin' LIMIT 1").fetchone()["id"]
    return create_session(admin_id, channel="dashboard")["session_id"]


def test_webhook_offers_the_handoff_resolved_event():
    assert "activity_handoff_resolved" in webhook_service.WEBHOOK_EVENT_TYPES
    sample = webhook_service._sample_payload("activity_handoff_resolved")
    assert sample["handoff"]["needs_human"] and sample["handoff_resolved_at"]


def test_agent_and_superseded_resolutions_reach_webhooks_and_the_change_feed(test_client, admin_token, monkeypatch):
    worker, _ = _setup()
    events = []
    monkeypatch.setattr(webhook_service, "dispatch_event", lambda event, payload: events.append((event, payload)))

    first = _waiting(A, "Pick a vendor")
    resolved = _mcp(test_client, worker, "activity_handoff_resolve", activity_id=first["id"], reason="Picked one")
    assert resolved.status_code == 200, resolved.json()
    event, payload = events[-1]
    assert event == "activity_handoff_resolved" and payload["handoff_resolution"] == "agent"

    older = _waiting(A, "Approve the old plan")
    with get_db() as conn:  # made before the next session started
        conn.execute(
            "UPDATE agent_activity SET handoff_requested_at = '2000-01-01T00:00:00+00:00', "
            "user_id = (SELECT id FROM users LIMIT 1) WHERE id = ?",
            (older["id"],),
        )
        conn.commit()
    _mcp(test_client, worker, "activity_update", memory_scope=A, task_description="New plan")
    closed = _mcp(test_client, worker, "activity_update", memory_scope=A, status="blocked",
                  handoff={"needs_human": "Approve the new plan"})
    assert [r["activity_id"] for r in closed.json()["data"]["superseded_requests"]] == [older["id"]]
    superseded = [p for e, p in events if e == "activity_handoff_resolved" and p.get("handoff_resolution") == "superseded"]
    assert [p["activity_id"] for p in superseded] == [older["id"]]

    with get_db() as conn:
        resolved_ids = {r[0] for r in conn.execute(
            "SELECT resource_id FROM workspace_changes WHERE memory_scope = ? AND change_type = 'handoff_resolved'", (A,)
        )}
    assert {first["id"], older["id"]} <= resolved_ids


def _suggestion(scope, suggestion_id):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO memory_proposals (id, rule, action, scope, target_ids_json, rationale, evidence_json, status, created_at) "
            "VALUES (?, 'unconfirmed_inference', 'confirm', ?, ?, 'private rationale text', '{}', 'pending', '2026-11-11T00:00:00+00:00')",
            (suggestion_id, scope, json.dumps([f"memory-of-{suggestion_id}"])),
        )


def _credential_request(scope, name):
    from app.services import credential_request_service

    credential_request_service.request_credential(
        agent_id="att-worker", user_id=None, scope=scope, name=name, purpose=f"needed for {name}"
    )


def test_attention_list_also_carries_credential_requests_and_memory_suggestions(test_client, admin_token):
    worker, coordinator = _setup()
    _waiting(A, "Approve the A release")
    _credential_request(A, "A_TOKEN")
    _credential_request(HIDDEN, "HIDDEN_TOKEN")
    _suggestion(B, "sug-b")
    _suggestion(HIDDEN, "sug-hidden")

    data = _mcp(test_client, coordinator, "attention_list").json()["data"]
    # The fields a reader already uses are unchanged.
    assert set(data) >= {"items", "count", "approvals"}
    assert data["count"] == 1 and data["items"][0]["needs_human"] == "Approve the A release"
    # The two new kinds arrive as new keys, limited to the scopes the caller reads.
    assert [r["name"] for r in data["credential_requests"]] == ["A_TOKEN"]
    assert data["credential_requests"][0]["requested_by"] == "att-worker"
    assert data["memory_suggestions"]["count"] == 1
    assert data["memory_suggestions"]["items"][0]["id"] == "sug-b"
    # A suggestion is named by rule and scope; the rationale (which can quote a memory) is not sent.
    assert "private rationale text" not in json.dumps(data)

    rest = test_client.get("/api/activity/attention", headers=coordinator).json()["data"]
    assert rest["credential_requests"] == data["credential_requests"]
    assert rest["memory_suggestions"] == data["memory_suggestions"]

    narrowed = _mcp(test_client, coordinator, "attention_list", scopes=[B]).json()["data"]
    assert narrowed["credential_requests"] == [] and narrowed["memory_suggestions"]["count"] == 1


def test_the_overview_and_attention_list_read_the_same_groups(test_client, admin_token):
    from app.services import waiting_service

    _setup()
    _waiting(A, "Approve the A release")
    _credential_request(A, "A_TOKEN")
    _suggestion(A, "sug-a")
    everything = waiting_service.waiting_on_person()
    assert len(everything["handoffs"]) == 1
    assert len(everything["credential_requests"]) == 1
    assert everything["memory_suggestions"]["count"] == 1
    only_handoffs = waiting_service.waiting_on_person(include=("handoffs",))
    assert only_handoffs["credential_requests"] == [] and only_handoffs["memory_suggestions"]["count"] == 0
    nothing = waiting_service.waiting_on_person(scopes=[])
    assert nothing["handoffs"] == [] and nothing["credential_requests"] == []
    assert nothing["memory_suggestions"]["count"] == 0
