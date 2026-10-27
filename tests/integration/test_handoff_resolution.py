"""Handoff requests to a person: superseding, agent resolution, reminders.

An agent's needs_human request used to wait for a person even after the same
agent dealt with it in a later session, since each session opens a new
activity and an agent can only update its current one.
"""

import json

from app.database import get_db
from app.services import activity_service, agent_service, workspace_service

SCOPE = "workspace:handoff-test"
OTHER_SCOPE = "workspace:handoff-other"


def _setup():
    with get_db() as conn:
        owner_id = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
    workspace_service.create_workspace("handoff-test", "Handoff Test", owner_id)
    workspace_service.create_workspace("handoff-other", "Handoff Other", owner_id)
    _, key = agent_service.create_agent(
        agent_id="h-agent", display_name="Handoff Agent", owner_user_id=owner_id,
        read_scopes=[SCOPE, OTHER_SCOPE], write_scopes=[SCOPE],
    )
    _, other_key = agent_service.create_agent(
        agent_id="h-other", display_name="Other Agent", owner_user_id=owner_id,
        read_scopes=[SCOPE], write_scopes=[SCOPE],
    )
    return owner_id, {"Authorization": f"Bearer {key}"}, {"Authorization": f"Bearer {other_key}"}


def _mcp(client, headers, tool, **params):
    return client.post("/mcp", headers=headers, json={"tool": tool, "params": params})


def _task(client, headers, description, handoff=None, status="completed", scope=SCOPE):
    """Open a task and close it, as one agent session would."""
    opened = _mcp(client, headers, "activity_update", memory_scope=scope, status="active",
                  task_description=description)
    assert opened.status_code in (200, 201), opened.json()
    params = {"memory_scope": scope, "status": status, "task_result": f"{description}: done"}
    if handoff is not None:
        params["handoff"] = handoff
    closed = _mcp(client, headers, "activity_update", **params)
    assert closed.status_code == 200, closed.json()
    return closed.json()["data"]


def _closed_request(user_id, description, handoff, agent_id="h-agent", scope=SCOPE):
    activity = activity_service.create_activity(
        agent_id=agent_id, user_id=user_id, task_description=description, memory_scope=scope,
    )
    activity_service.set_handoff(activity["id"], handoff)
    activity_service.update_activity(activity["id"], status="completed")
    return activity


def _handoff(activity_id):
    row = activity_service.get_activity(activity_id)
    return json.loads(row["handoff_json"]), row["handoff_resolved_at"]


def _audit(action):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT actor_type, actor_id, resource_id, details_json FROM audit_log WHERE action = ? ORDER BY id",
            (action,),
        ).fetchall()
    return [{**dict(r), "details": json.loads(r["details_json"] or "{}")} for r in rows]


def _as_person(client, owner_id):
    from app.services.auth_service import create_session

    client.cookies.set("session_token", create_session(owner_id, channel="dashboard")["session_id"])


def test_new_request_supersedes_older_ones_and_agent_restates_one(test_client, admin_token):
    owner_id, headers, _ = _setup()
    # Two unrelated open requests, as recorded before superseding existed (a
    # second one through activity_update would supersede the first).
    first = _closed_request(owner_id, "Set up the vendor integration", {
        "completed": "Integration stubbed.",
        "open_questions": ["Which vendor tier?"],
        "needs_human": "Approve the vendor contract.",
    })
    second = _closed_request(owner_id, "Rotate the deploy credentials", {
        "needs_human": "Rotate the API key in the provider console.",
    })
    assert {w["id"] for w in activity_service.list_needs_human()} == {first["id"], second["id"]}

    closed = _task(test_client, headers, "Prepare the release", {
        "completed": "Release branch cut.",
        "needs_human": "Review the release notes.",
    })
    third = closed["activity"]

    listed = {r["activity_id"]: r for r in closed["superseded_requests"]}
    assert set(listed) == {first["id"], second["id"]}
    assert listed[first["id"]]["summary"] == "Set up the vendor integration"
    assert listed[first["id"]]["needs_human"] == "Approve the vendor contract."
    assert listed[first["id"]]["open_questions"] == ["Which vendor tier?"]
    assert listed[second["id"]]["needs_human"] == "Rotate the API key in the provider console."
    assert "open_questions" not in listed[second["id"]]
    assert [w["code"] for w in closed["warnings"]] == ["HANDOFF_REQUESTS_SUPERSEDED"]

    for old in (first, second):
        handoff, resolved_at = _handoff(old["id"])
        assert resolved_at
        assert handoff["resolution"]["kind"] == "superseded"
        assert handoff["resolution"]["superseded_by"] == third["id"]
        assert handoff["resolution"]["resolved_by"] == "h-agent"
    audits = _audit("handoff_superseded")
    assert {a["resource_id"] for a in audits} == {first["id"], second["id"]}
    assert all(a["details"]["superseded_by"] == third["id"] for a in audits)
    assert all(a["actor_type"] == "agent" for a in audits)

    # The contract still needs a person: the agent restates it in the same
    # session by amending the handoff it just closed with.
    restated = _mcp(test_client, headers, "activity_update", memory_scope=SCOPE, handoff={
        "completed": "Release branch cut.",
        "needs_human": "Review the release notes. Still open from earlier: approve the vendor contract.",
    })
    assert restated.status_code == 200, restated.json()
    assert restated.json()["data"]["handoff_amended"] is True
    assert restated.json()["data"]["activity"]["id"] == third["id"]
    assert "superseded_requests" not in restated.json()["data"]

    waiting = activity_service.list_needs_human()
    assert [w["id"] for w in waiting] == [third["id"]]
    assert "approve the vendor contract" in waiting[0]["needs_human"]
    assert _handoff(second["id"])[1]  # the key rotation stays superseded

    _as_person(test_client, owner_id)
    page = test_client.get("/activity").text
    test_client.cookies.clear()
    queue, superseded_group = page.split("<details class='handoff-superseded'", 1)
    assert "approve the vendor contract" in queue
    assert f"data-handoff-resolve='{third['id']}'" in queue
    assert "Rotate the API key in the provider console." not in queue
    assert "Superseded by a later handoff (2)" in superseded_group
    assert "Rotate the API key in the provider console." in superseded_group
    assert third["id"] in superseded_group


def test_superseding_stays_within_the_agent_scope_and_user(test_client, admin_token):
    owner_id, headers, other_headers = _setup()
    other_scope = activity_service.create_activity(
        agent_id="h-agent", user_id=owner_id, task_description="Other workspace", memory_scope=OTHER_SCOPE,
    )
    other_user = activity_service.create_activity(
        agent_id="h-agent", user_id="someone-else", task_description="Other user", memory_scope=SCOPE,
    )
    other_agent = _task(test_client, other_headers, "Another agent's task", {"needs_human": "Answer h-other."})
    for activity in (other_scope, other_user):
        activity_service.set_handoff(activity["id"], {"needs_human": "Keep me open."})
        activity_service.update_activity(activity["id"], status="completed")

    closed = _task(test_client, headers, "New task", {"needs_human": "New request."})
    assert "superseded_requests" not in closed
    for activity_id in (other_scope["id"], other_user["id"], other_agent["activity"]["id"]):
        assert not _handoff(activity_id)[1]
    assert not _audit("handoff_superseded")


def test_a_handoff_without_a_request_supersedes_nothing(test_client, admin_token):
    _, headers, _ = _setup()
    first = _task(test_client, headers, "First", {"needs_human": "Approve it."})["activity"]
    second = _task(test_client, headers, "Second", {"next_steps": ["carry on"]}, status="blocked")
    assert "superseded_requests" not in second
    assert not _handoff(first["id"])[1]


def test_closing_without_a_request_reminds_and_resolves_nothing(test_client, admin_token):
    owner_id, headers, _ = _setup()
    first = _closed_request(owner_id, "First", {"needs_human": "Approve the budget."})
    second = _task(test_client, headers, "Second", {"needs_human": "Pick a region."},
                   status="blocked")["activity"]
    assert _handoff(first["id"])[1]  # superseded by the second

    third = _task(test_client, headers, "Third")
    assert [r["activity_id"] for r in third["open_requests"]] == [second["id"]]
    assert third["open_requests"][0]["needs_human"] == "Pick a region."
    codes = [w["code"] for w in third["warnings"]]
    assert codes == ["HANDOFF_MISSING", "OPEN_HANDOFF_REQUESTS"]
    assert "activity_handoff_resolve" in third["warnings"][1]["message"]
    assert not _handoff(second["id"])[1]

    fourth = _task(test_client, headers, "Fourth", {"completed": "Done.", "next_steps": ["x"]})
    assert [r["activity_id"] for r in fourth["open_requests"]] == [second["id"]]
    assert [w["code"] for w in fourth["warnings"]] == ["OPEN_HANDOFF_REQUESTS"]
    assert not _handoff(second["id"])[1]
    assert [w["id"] for w in activity_service.list_needs_human()] == [second["id"]]

    # Heartbeats and progress notes carry no reminder.
    _mcp(test_client, headers, "activity_update", memory_scope=SCOPE, status="active", task_description="Fifth")
    note = _mcp(test_client, headers, "activity_update", memory_scope=SCOPE, task_note="halfway").json()["data"]
    assert "open_requests" not in note


def test_agent_resolves_its_own_request_with_a_reason(test_client, admin_token):
    _, headers, _ = _setup()
    activity = _task(test_client, headers, "Wire billing", {"needs_human": "Create the Stripe account."})["activity"]

    response = _mcp(test_client, headers, "activity_handoff_resolve",
                    activity_id=activity["id"], reason="  Brian created it; keys are stored.  ")
    assert response.status_code == 200, response.json()
    resolution = response.json()["data"]["resolution"]
    assert resolution["kind"] == "agent" and resolution["resolved_by"] == "h-agent"
    assert resolution["reason"] == "Brian created it; keys are stored."
    handoff, resolved_at = _handoff(activity["id"])
    assert resolved_at and handoff["resolution"] == resolution
    assert handoff["needs_human"] == "Create the Stripe account."
    assert not activity_service.list_needs_human()

    audit = _audit("handoff_resolved")[-1]
    assert audit["actor_type"] == "agent" and audit["resource_id"] == activity["id"]
    assert audit["details"]["kind"] == "agent"
    assert audit["details"]["reason"] == "Brian created it; keys are stored."

    again = _mcp(test_client, headers, "activity_handoff_resolve", activity_id=activity["id"], reason="again")
    assert again.status_code == 409 and again.json()["error"]["code"] == "NOT_PENDING"


def test_agent_cannot_resolve_another_agents_request_or_outside_its_scopes(test_client, admin_token):
    owner_id, headers, other_headers = _setup()
    theirs = _task(test_client, other_headers, "Their task", {"needs_human": "Theirs."})["activity"]
    refused = _mcp(test_client, headers, "activity_handoff_resolve", activity_id=theirs["id"], reason="not mine")
    assert refused.status_code == 403
    assert not _handoff(theirs["id"])[1]

    # Its own activity, but in a scope it can only read.
    read_only = activity_service.create_activity(
        agent_id="h-agent", user_id=owner_id, task_description="Read-only", memory_scope=OTHER_SCOPE,
    )
    activity_service.set_handoff(read_only["id"], {"needs_human": "Something."})
    refused = _mcp(test_client, headers, "activity_handoff_resolve", activity_id=read_only["id"], reason="x")
    assert refused.status_code == 403
    assert not _handoff(read_only["id"])[1]

    missing = _mcp(test_client, headers, "activity_handoff_resolve", activity_id="nope", reason="x")
    assert missing.status_code == 404


def test_resolving_requires_a_bounded_reason(test_client, admin_token):
    _, headers, _ = _setup()
    activity = _task(test_client, headers, "Task", {"needs_human": "Do it."})["activity"]
    for params in ({}, {"reason": ""}, {"reason": "   "}, {"reason": "x" * 501}):
        response = _mcp(test_client, headers, "activity_handoff_resolve", activity_id=activity["id"], **params)
        assert response.status_code == 400, params
        assert response.json()["error"]["code"] == "INVALID_PARAMS"
    assert not _handoff(activity["id"])[1]


def test_person_resolves_with_kind_person_and_agents_cannot_use_the_person_route(test_client, admin_token):
    owner_id, headers, _ = _setup()
    activity = _task(test_client, headers, "Task", {"needs_human": "Sign the form."})["activity"]

    assert test_client.post(f"/api/activity/{activity['id']}/handoff/resolve", headers=headers).status_code == 403
    assert not _handoff(activity["id"])[1]

    _as_person(test_client, owner_id)
    response = test_client.post(f"/api/activity/{activity['id']}/handoff/resolve")
    again = test_client.post(f"/api/activity/{activity['id']}/handoff/resolve")
    test_client.cookies.clear()
    assert response.status_code == 200
    resolution = response.json()["data"]["resolution"]
    assert resolution["kind"] == "person" and resolution["resolved_by"] == owner_id
    assert "reason" not in resolution
    assert _handoff(activity["id"])[0]["resolution"] == resolution
    assert again.status_code == 409
    audit = _audit("handoff_resolved")[-1]
    assert audit["actor_type"] == "user" and audit["details"]["kind"] == "person"
    assert audit["details"]["activity_id"] == activity["id"]


def test_an_agent_cannot_forge_a_resolution(test_client, admin_token):
    _, headers, _ = _setup()
    _mcp(test_client, headers, "activity_update", memory_scope=SCOPE, status="active", task_description="t")
    forged = _mcp(test_client, headers, "activity_update", memory_scope=SCOPE, status="completed", handoff={
        "needs_human": "Approve it.", "resolution": {"kind": "person", "at": "2026-01-01T00:00:00+00:00"},
    })
    assert forged.status_code == 400 and forged.json()["error"]["code"] == "INVALID_HANDOFF"


def test_resolution_survives_an_identical_request_and_clears_on_a_new_one(test_client, admin_token):
    activity = activity_service.create_activity(agent_id="a", user_id="u", task_description="t", memory_scope=SCOPE)
    activity_service.set_handoff(activity["id"], {"needs_human": "Approve it."})
    resolution = activity_service.resolve_handoff(activity["id"], kind="person", resolved_by="u")
    activity_service.set_handoff(activity["id"], {"needs_human": "Approve it.", "next_steps": ["x"]})
    handoff, resolved_at = _handoff(activity["id"])
    assert resolved_at and handoff["resolution"] == resolution and handoff["next_steps"] == ["x"]
    activity_service.set_handoff(activity["id"], {"needs_human": "Sign the contract."})
    handoff, resolved_at = _handoff(activity["id"])
    assert not resolved_at and "resolution" not in handoff
    # A handoff with nothing for a person has no request to resolve.
    plain = activity_service.create_activity(agent_id="a", user_id="u", task_description="p", memory_scope=SCOPE)
    activity_service.set_handoff(plain["id"], {"next_steps": ["x"]})
    assert activity_service.resolve_handoff(plain["id"]) is None


def test_a_handoff_alone_amends_only_the_latest_closed_task(test_client, admin_token):
    _, headers, _ = _setup()
    # No task yet: a handoff alone still needs a task description.
    response = _mcp(test_client, headers, "activity_update", memory_scope=SCOPE, handoff={"next_steps": ["x"]})
    assert response.status_code == 400 and response.json()["error"]["code"] == "TASK_REQUIRED"

    closed = _task(test_client, headers, "Closed task")["activity"]
    _mcp(test_client, headers, "activity_update", memory_scope=SCOPE, status="active", task_description="Open")
    _mcp(test_client, headers, "activity_update", memory_scope=SCOPE, status="cancelled")
    response = _mcp(test_client, headers, "activity_update", memory_scope=SCOPE, handoff={"next_steps": ["x"]})
    assert response.status_code == 400  # the latest task was cancelled, not closed with a handoff
    assert activity_service.get_activity(closed["id"])["handoff_json"] is None


def test_session_context_shows_the_agent_its_own_open_requests(test_client, admin_token):
    owner_id, headers, other_headers = _setup()
    mine = activity_service.create_activity(
        agent_id="h-agent", user_id=owner_id, task_description="Mine", memory_scope=SCOPE,
    )
    activity_service.set_handoff(mine["id"], {"needs_human": "My own request."})
    activity_service.update_activity(mine["id"], status="completed")
    with get_db() as conn:  # older than everything below
        conn.execute("UPDATE agent_activity SET updated_at = '2026-01-01T00:00:00+00:00' WHERE id = ?", (mine["id"],))
        conn.commit()
    for index in range(6):  # more than the scope-wide list shows
        theirs = activity_service.create_activity(
            agent_id=f"agent-{index}", user_id=owner_id, task_description=f"Theirs {index}", memory_scope=SCOPE,
        )
        activity_service.set_handoff(theirs["id"], {"needs_human": f"Their request {index}."})

    text = test_client.post("/api/session-context", headers=headers,
                            json={"memory_scope": SCOPE}).json()["data"]["context"]
    waiting = text[text.index("## Waiting on a person"):]
    assert f"{mine['id']} (h-agent, yours): My own request." in waiting
    assert "activity_handoff_resolve(activity_id, reason)" in waiting
    assert "Their request 5." in waiting

    other = test_client.post("/api/session-context", headers=other_headers,
                             json={"memory_scope": SCOPE}).json()["data"]["context"]
    assert "My own request." not in other  # past the scope-wide limit, and not theirs
    assert "activity_handoff_resolve" not in other
