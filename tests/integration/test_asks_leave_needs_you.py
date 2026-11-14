"""Needs you holds only what a person can still act on.

A scripted day in which every ask is answered, overtaken or closed with its task; at the end attention_list is empty.
One ask stays, to prove the list is not simply always empty.
"""

from datetime import timedelta

from app.database import get_db
from app.services import activity_service, agent_service, workspace_service
from app.services.activity_service import utc_now
from tests.conftest import csrf_headers

WS = "workspace:asks-a"


def _mcp(client, key, tool, **params):
    r = client.post("/mcp", headers={"Authorization": f"Bearer {key}"}, json={"tool": tool, "params": params})
    assert r.status_code in (200, 201), (tool, r.text)
    return r.json()["data"]


def _setup():
    with get_db() as conn:
        owner = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
    workspace_service.create_workspace("asks-a", "asks-a", owner)
    _, key = agent_service.create_agent(
        agent_id="builder", display_name="builder", owner_user_id=owner,
        read_scopes=[WS, "agent:builder"], write_scopes=[WS, "agent:builder"],
    )
    return owner, key


def _waiting(client, key):
    return _mcp(client, key, "attention_list")["items"]


def _blocked_with_ask(client, key, task, ask, ref, *, block=True):
    ctx = _mcp(client, key, "session_context", memory_scope=WS, host_session_ref=f"s-{ref}")
    ex = ctx["execution_id"]
    _mcp(client, key, "activity_update", memory_scope=WS, task_description=task, status="active",
         task_ref=f"builder:{ref}", execution_id=ex)
    out = _mcp(client, key, "activity_update", memory_scope=WS, task_result="Waiting on a decision",
               handoff={"needs_human": ask}, task_ref=f"builder:{ref}", execution_id=ex,
               **({"status": "blocked"} if block else {}))
    return out["activity"]["id"]


def test_a_day_of_asks_ends_with_an_empty_needs_you_page(test_client, admin_token):
    owner, key = _setup()
    merge_a = _blocked_with_ask(test_client, key, "Merge candidate A", "Approve the merge.", "a")
    merge_b = _blocked_with_ask(test_client, key, "Merge candidate B", "Approve the merge.", "b")
    spec = _blocked_with_ask(test_client, key, "Spec question", "Answer the spec's question.", "c", block=False)  # still open
    plan = _blocked_with_ask(test_client, key, "Plan review", "Arrange a read-only review.", "d")
    old = _blocked_with_ask(test_client, key, "Old question", "Pick a colour.", "e")
    keep = _blocked_with_ask(test_client, key, "Still open", "Decide the budget.", "f")
    assert len(_waiting(test_client, key)) == 6

    # The agent saw the merge happen: it resolves its own ask, then closes with a result.
    _mcp(test_client, key, "activity_handoff_resolve", activity_id=merge_a, reason="Merged at 10:02")
    # The agent closes a blocked record with its ask still open: closing ends the ask.
    closed = _mcp(test_client, key, "activity_update", activity_id=merge_b, status="completed",
                  task_result="Merged; nothing left to do")
    assert closed["resolved_requests"][0]["needs_human"] == "Approve the merge."
    assert closed["warnings"][0]["code"] == "HANDOFF_ASK_CLOSED"
    # An admin finishing a record off, over REST: the ask is ended as a person's resolution.
    r = test_client.put(f"/api/activity/{plan}", headers=csrf_headers(test_client),
                        json={"status": "completed", "task_result": "Housekeeping: plan done elsewhere"})
    assert r.status_code == 200, r.text
    # A person cancelling a task from the dashboard.
    r = test_client.delete(f"/api/activity/{spec}", headers=csrf_headers(test_client))
    assert r.status_code == 200, r.text
    # Nobody ever answers one: it ages out.
    long_ago = (utc_now() - timedelta(days=9)).isoformat()
    with get_db() as conn:
        conn.execute("UPDATE agent_activity SET updated_at = ? WHERE id = ?", (long_ago, old))
        conn.commit()
    assert activity_service.expire_unanswered_asks() == 1

    items = _waiting(test_client, key)
    assert [i["id"] for i in items] == [keep]

    # The one left is answered by a person; the page is empty.
    r = test_client.post(f"/api/activity/{keep}/handoff/resolve", headers=csrf_headers(test_client))
    assert r.status_code == 200, r.text
    assert _waiting(test_client, key) == []
    kinds = {
        row["id"]: activity_service.load_handoff(row["handoff_json"]).get("resolution", {}).get("kind")
        for row in _rows([merge_a, merge_b, spec, plan, old, keep])
    }
    assert kinds == {merge_a: "agent", merge_b: "agent", spec: "person", plan: "person", old: "expired", keep: "person"}


def _rows(ids):
    with get_db() as conn:
        return conn.execute(
            f"SELECT id, handoff_json FROM agent_activity WHERE id IN ({','.join('?' for _ in ids)})", ids
        ).fetchall()


def test_completing_with_a_new_ask_keeps_that_ask(test_client, admin_token):
    _, key = _setup()
    ctx = _mcp(test_client, key, "session_context", memory_scope=WS, host_session_ref="s-x")
    ex = ctx["execution_id"]
    _mcp(test_client, key, "activity_update", memory_scope=WS, task_description="Ship it", status="active", execution_id=ex)
    out = _mcp(test_client, key, "activity_update", memory_scope=WS, status="completed", task_result="Shipped",
               handoff={"needs_human": "Approve the release notes."}, execution_id=ex)
    assert "resolved_requests" not in out
    assert [i["id"] for i in _waiting(test_client, key)] == [out["activity"]["id"]]


def test_an_open_task_that_is_abandoned_takes_its_ask_with_it(test_client, admin_token):
    _, key = _setup()
    ctx = _mcp(test_client, key, "session_context", memory_scope=WS, host_session_ref="s-y")
    ex = ctx["execution_id"]
    _mcp(test_client, key, "activity_update", memory_scope=WS, task_description="Waits quietly", status="active", execution_id=ex)
    out = _mcp(test_client, key, "activity_update", memory_scope=WS, task_note="asking", handoff={"needs_human": "Pick one."},
               execution_id=ex)
    activity_id = out["activity"]["id"]
    assert len(_waiting(test_client, key)) == 1
    long_ago = (utc_now() - timedelta(hours=60)).isoformat()
    with get_db() as conn:
        conn.execute("UPDATE agent_activity SET heartbeat_at = ?, updated_at = ? WHERE id = ?", (long_ago, long_ago, activity_id))
        conn.commit()
    assert activity_service.close_abandoned_activities() == 1
    assert _waiting(test_client, key) == []


def test_an_ask_that_says_no_action_is_not_raised(test_client, admin_token):
    _, key = _setup()
    ctx = _mcp(test_client, key, "session_context", memory_scope=WS, host_session_ref="s-z")
    ex = ctx["execution_id"]
    _mcp(test_client, key, "activity_update", memory_scope=WS, task_description="Report", status="active", execution_id=ex)
    text = "No action needed to finish reporting. Any remediation requires a separate follow-up."
    out = _mcp(test_client, key, "activity_update", memory_scope=WS, status="completed", task_result="Reported",
               handoff={"needs_human": text}, execution_id=ex)
    assert [w["code"] for w in out["warnings"]] == ["NEEDS_HUMAN_STATES_NO_ACTION"]
    assert _waiting(test_client, key) == []
    stored = activity_service.load_handoff(activity_service.get_activity(out["activity"]["id"])["handoff_json"])
    assert text in stored["open_questions"] and "needs_human" not in stored
    # A real ask that merely mentions "no" is kept.
    ctx2 = _mcp(test_client, key, "session_context", memory_scope=WS, host_session_ref="s-z2")
    _mcp(test_client, key, "activity_update", memory_scope=WS, task_description="Other", status="active",
         execution_id=ctx2["execution_id"])
    kept = _mcp(test_client, key, "activity_update", memory_scope=WS, status="completed", task_result="Done",
                handoff={"needs_human": "Nothing ships until you approve the budget."}, execution_id=ctx2["execution_id"])
    assert "warnings" not in kept or all(w["code"] != "NEEDS_HUMAN_STATES_NO_ACTION" for w in kept["warnings"])
    assert len(_waiting(test_client, key)) == 1


def test_a_blocked_task_with_nothing_waiting_ends_after_seven_days(test_client, admin_token):
    _, key = _setup()
    quiet = _blocked_with_ask(test_client, key, "Quiet", "Pick.", "q")        # ask lapses, then the task ends
    answered = _blocked_with_ask(test_client, key, "Answered", "Pick.", "r")  # answered long ago, never closed
    fresh_answer = _blocked_with_ask(test_client, key, "Just answered", "Pick.", "s")
    recent = _blocked_with_ask(test_client, key, "Recent", "Pick.", "t")      # still within the week
    _mcp(test_client, key, "activity_handoff_resolve", activity_id=answered, reason="Done")
    _mcp(test_client, key, "activity_handoff_resolve", activity_id=fresh_answer, reason="Done")
    old = (utc_now() - timedelta(days=9)).isoformat()
    last = old
    with get_db() as conn:
        for activity_id in (quiet, answered, fresh_answer):
            conn.execute("UPDATE agent_activity SET updated_at = ? WHERE id = ?", (old, activity_id))
        conn.execute("UPDATE agent_activity SET handoff_resolved_at = ? WHERE id = ?", (old, answered))
        conn.commit()
    activity_service.mark_stale_activities()
    rows = {a: activity_service.get_activity(a) for a in (quiet, answered, fresh_answer, recent)}
    assert rows[quiet]["status"] == rows[answered]["status"] == "cancelled"
    assert rows[quiet]["task_result"] == "Waiting on a decision"  # an existing result is kept
    assert rows[quiet]["ended_at"] == last
    assert rows[fresh_answer]["status"] == "blocked"  # answered a moment ago: that is a change
    assert rows[recent]["status"] == "blocked"
    assert [i["id"] for i in _waiting(test_client, key)] == [recent]
    # With no result recorded the wording is the abandoned rule's.
    bare = _blocked_with_ask(test_client, key, "Bare", "Pick.", "u")
    with get_db() as conn:
        conn.execute("UPDATE agent_activity SET updated_at = ?, task_result = NULL WHERE id = ?", (old, bare))
        conn.commit()
    activity_service.mark_stale_activities()  # the ask lapses, then the task ends in the same pass
    assert activity_service.get_activity(bare)["task_result"] == "Ended without report: no update for 7 days."
    assert activity_service.end_unattended_blocked(0) == 0


def test_the_expiry_can_be_turned_off(test_client, admin_token):
    _, key = _setup()
    old = _blocked_with_ask(test_client, key, "Old", "Pick.", "o")
    long_ago = (utc_now() - timedelta(days=30)).isoformat()
    with get_db() as conn:
        conn.execute("UPDATE agent_activity SET updated_at = ? WHERE id = ?", (long_ago, old))
        conn.commit()
    assert activity_service.expire_unanswered_asks(0) == 0
    assert len(_waiting(test_client, key)) == 1


def test_an_older_ask_that_only_says_nothing_is_needed_stops_waiting(test_client, admin_token):
    _, key = _setup()
    fake = _blocked_with_ask(test_client, key, "Reported", "Pick.", "v")
    real = _blocked_with_ask(test_client, key, "Real", "Nothing ships until you approve the budget.", "w")
    import json
    with get_db() as conn:  # records written before the write-time check existed
        row = conn.execute("SELECT handoff_json FROM agent_activity WHERE id = ?", (fake,)).fetchone()
        stored = json.loads(row["handoff_json"])
        stored["needs_human"] = "Nothing now"
        conn.execute("UPDATE agent_activity SET handoff_json = ? WHERE id = ?", (json.dumps(stored), fake))
        conn.commit()
    assert {i["id"] for i in _waiting(test_client, key)} == {fake, real}
    assert activity_service.expire_unanswered_asks() == 1
    assert [i["id"] for i in _waiting(test_client, key)] == [real]
    resolution = activity_service.load_handoff(activity_service.get_activity(fake)["handoff_json"])["resolution"]
    assert resolution["kind"] == "expired"
