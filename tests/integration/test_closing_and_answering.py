"""Closing a record that ended blocked, and closing a suggestion that someone has already answered."""

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

def _memory(scope=WS, content="The build server is 192.0.2.10"):
    record, _ = memory_service.write_memory(content=content, memory_class="fact", scope=scope)
    return record["id"]


def _suggest(record_id, rule="unconfirmed_inference", action="confirm", suggestion_id=None, targets=None):
    suggestion_id = suggestion_id or f"sug-{record_id}-{rule}"
    with get_db() as conn:
        conn.execute(
            "INSERT INTO memory_proposals (id, rule, action, scope, target_ids_json, rationale, evidence_json, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'r', '{}', 'pending', '2026-11-08T00:00:00+00:00')",
            (suggestion_id, rule, action, WS, json.dumps(targets or [record_id])),
        )
        conn.commit()
    return suggestion_id


def _status(suggestion_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT status, decided_by FROM memory_proposals WHERE id = ?", (suggestion_id,)
        ).fetchone()
    return row["status"], row["decided_by"]


def test_confirming_a_memory_with_evidence_answers_its_pending_suggestions(test_client, admin_token):
    owner, _ = _setup()
    record_id = _memory()
    first = _suggest(record_id, "unconfirmed_inference")
    second = _suggest(record_id, "stale_volatile")
    r = test_client.post("/mcp", headers=owner, json={"tool": "memory_confirm", "params": {
        "record_id": record_id, "evidence": "ssh nexus: ip a shows 192.0.2.10"}})
    assert r.status_code == 200 and r.json()["ok"], r.text
    assert _status(first) == ("accepted", "close-owner") and _status(second) == ("accepted", "close-owner")
    from app.services import audit_service

    answered = audit_service.query_events(action="memory_proposal_answered", limit=10)
    assert len(answered) == 2 and "ssh nexus" in str(answered[0]) and answered[0]["actor_id"] == "close-owner"
    # They left the person's list but stay in the history.
    from app.services import memory_proposal_service

    assert memory_proposal_service.pending_summary([WS])["count"] == 0
    assert any(p["id"] == first for p in memory_proposal_service.list_proposals(status="accepted"))


def test_confirming_without_evidence_closes_nothing(test_client, admin_token):
    owner, _ = _setup()
    record_id = _memory()
    suggestion = _suggest(record_id)
    for evidence in ("", "   "):
        r = test_client.post("/mcp", headers=owner, json={"tool": "memory_confirm", "params": {
            "record_id": record_id, "evidence": evidence}})
        assert not r.json().get("ok", False)
    assert _status(suggestion) == ("pending", None)
    assert memory_service.get_memory_record(record_id)["last_confirmed_at"] is None


def test_retracting_with_a_reason_answers_suggestions_and_without_one_does_not(test_client, admin_token):
    owner, _ = _setup()
    kept, gone = _memory(content="Kept fact"), _memory(content="Gone fact")
    s_kept, s_gone = _suggest(kept), _suggest(gone)
    r = test_client.post("/mcp", headers=owner, json={"tool": "memory_retract", "params": {"record_id": kept}})
    assert r.json()["ok"] and _status(s_kept) == ("pending", None)
    r = test_client.post("/mcp", headers=owner, json={"tool": "memory_retract", "params": {
        "record_id": gone, "reason": "the host was decommissioned"}})
    assert r.json()["ok"] and _status(s_gone) == ("accepted", "close-owner")


def test_a_suggestion_to_retract_is_a_miss_when_the_memory_is_checked_and_kept(test_client, admin_token):
    owner, _ = _setup()
    record_id = _memory()
    suggestion = _suggest(record_id, rule="episodic_log", action="retract")
    test_client.post("/mcp", headers=owner, json={"tool": "memory_confirm", "params": {
        "record_id": record_id, "evidence": "still describes the live service"}})
    assert _status(suggestion) == ("rejected", "close-owner")


def test_clusters_and_pin_requests_are_left_for_a_person(test_client, admin_token):
    owner, _ = _setup()
    a, b = _memory(content="dup one"), _memory(content="dup two")
    cluster = _suggest(a, rule="duplicate_cluster", action="retract", targets=[a, b])
    pin = _suggest(b, rule="pin_request", action="pin")
    for rid in (a, b):
        test_client.post("/mcp", headers=owner, json={"tool": "memory_confirm", "params": {
            "record_id": rid, "evidence": "checked"}})
    assert _status(cluster) == ("pending", None) and _status(pin) == ("pending", None)
