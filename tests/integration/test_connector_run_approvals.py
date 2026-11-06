"""A service action a person has asked to approve first, and every way of not getting round that."""

import json
import time

import pytest

from app.services import auth_service, connector_service, credential_service
from tests.conftest import csrf_headers
from tests.integration.test_approvals import _agent, _browser_admin, _workspace

SERVICE = "gate-svc"


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _mcp(client, token, tool, params):
    return client.post("/mcp", headers=_bearer(token), json={"tool": tool, "params": params})


@pytest.fixture
def world(test_client, admin_token, monkeypatch):
    """A service with two actions, a binding in workspace:ops, two agents, and a fake executor."""
    _workspace(test_client, admin_token)
    caps = ["memory", "coordination", "connectors_read", "connectors_execute", "admin_propose"]
    runner = _agent(test_client, admin_token, "runner", caps)
    other = _agent(test_client, admin_token, "other", caps)
    connector_service.create_connector_type(
        connector_type_id=SERVICE, display_name="Gate Service", auth_type="none",
        supported_actions=[{"name": "do_thing", "method": "POST"}, {"name": "read_thing", "method": "GET"}],
        backend_type="generic_http",
    )
    credential = credential_service.create_credential("workspace:ops", "gate-cred", "test-secret", created_by="admin")
    binding = connector_service.create_binding(
        SERVICE, "Gate binding", "workspace:ops", credential_id=credential["id"], created_by="admin")
    calls = []

    def fake_execute(binding_id, action, params=None):
        calls.append((binding_id, action, params or {}))
        return {"success": True, "body": {"did": action}, "action": action}

    monkeypatch.setattr(connector_service, "execute_binding_action", fake_execute)
    return {"runner": runner, "other": other, "binding": binding, "calls": calls, "client": test_client,
            "admin_token": admin_token}


def _gate(world, action="do_thing", enabled=True, actors="all"):
    client = world["client"]
    _browser_admin(client)
    r = client.put(f"/api/connector-types/{SERVICE}/gates/{action}", headers=csrf_headers(client),
                   json={"enabled": enabled, "actors": actors})
    assert r.status_code == 200, r.text
    client.cookies.clear()
    return r


def _run(world, key, action="do_thing", params=None):
    return _mcp(world["client"], key, "connectors_run",
                {"binding_id": world["binding"]["id"], "action": action, "params": params or {"x": 1}})


def _wait_for(client, approval_id, wanted, timeout=8):
    from app.services import approval_service

    deadline = time.time() + timeout
    while time.time() < deadline:
        row = approval_service.get(approval_id)
        if row["status"] in wanted:
            return row
        time.sleep(0.05)
    raise AssertionError(f"approval stayed {approval_service.get(approval_id)['status']}")


def _decide(world, approval_id, approve=True, note=None):
    client = world["client"]
    _browser_admin(client)
    return client.post(f"/api/approvals/{approval_id}/decide", headers=csrf_headers(client),
                       json={"approve": approve, "note": note})


def test_nothing_is_held_until_a_person_turns_a_gate_on(world):
    r = _run(world, world["runner"])
    assert r.json()["data"]["success"] is True
    assert len(world["calls"]) == 1


def test_a_held_action_does_not_run_and_tells_the_agent_to_wait(world):
    _gate(world)
    data = _run(world, world["runner"]).json()["data"]
    assert data["pending_approval"] is True and data["status"] == "pending" and data["approval_id"]
    assert "Do not retry" in data["message"] and data["expires_at"]
    assert world["calls"] == []
    # An action that is not gated still runs.
    assert _run(world, world["runner"], action="read_thing").json()["data"]["success"] is True
    # Asking again for the very same thing does not pile up a second request.
    assert _run(world, world["runner"]).json()["data"]["approval_id"] == data["approval_id"]


def test_approving_runs_the_exact_request_once_and_the_agent_reads_the_result(world):
    _gate(world)
    held = _run(world, world["runner"], params={"title": "hello"}).json()["data"]
    approval_id = held["approval_id"]
    assert _decide(world, approval_id).status_code == 200
    done = _wait_for(world["client"], approval_id, ("done", "failed"))
    assert done["status"] == "done"
    assert world["calls"] == [(world["binding"]["id"], "do_thing", {"title": "hello"})]
    view = _mcp(world["client"], world["runner"], "approval_get", {"approval_id": approval_id}).json()["data"]["approval"]
    assert view["status"] == "done" and view["result"]["body"] == {"did": "do_thing"}
    # Deciding again, or replaying the approval, never runs it a second time.
    assert _decide(world, approval_id).status_code == 409
    assert len(world["calls"]) == 1


def test_a_rejection_reaches_the_agent_with_the_note_and_nothing_runs(world):
    _gate(world)
    approval_id = _run(world, world["runner"]).json()["data"]["approval_id"]
    assert _decide(world, approval_id, approve=False, note="Not today").status_code == 200
    view = _mcp(world["client"], world["runner"], "approval_get", {"approval_id": approval_id}).json()["data"]["approval"]
    assert view["status"] == "rejected" and view["decision_note"] == "Not today"
    assert "Do not retry" in view["message"]
    assert world["calls"] == []


def test_an_expired_request_cannot_be_approved(world):
    from app.database import get_db

    _gate(world)
    approval_id = _run(world, world["runner"]).json()["data"]["approval_id"]
    with get_db() as conn:
        conn.execute("UPDATE approvals SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (approval_id,))
        conn.commit()
    assert _decide(world, approval_id).status_code == 409
    view = _mcp(world["client"], world["runner"], "approval_get", {"approval_id": approval_id}).json()["data"]["approval"]
    assert view["status"] == "expired" and "Do not retry" in view["message"]
    assert world["calls"] == []


def test_a_binding_changed_after_the_request_fails_instead_of_running(world):
    _gate(world)
    approval_id = _run(world, world["runner"]).json()["data"]["approval_id"]
    connector_service.update_binding(world["binding"]["id"], config_json=json.dumps({"base_url": "https://elsewhere.example"}))
    assert _decide(world, approval_id).status_code == 200
    row = _wait_for(world["client"], approval_id, ("done", "failed"))
    assert row["status"] == "failed" and "changed after this was requested" in row["error"]
    assert world["calls"] == []


def test_every_key_token_and_script_is_held_but_a_person_in_the_dashboard_is_not(world):
    client = world["client"]
    _gate(world)
    # An agent key.
    assert _run(world, world["runner"]).json()["data"]["pending_approval"] is True
    # An operator token on the REST run route.
    token, _ = auth_service.create_operator_token("admin", 15)
    r = client.post(f"/api/connector-bindings/{world['binding']['id']}/run", headers=_bearer(token),
                    json={"action": "do_thing", "params": {"y": 2}})
    assert r.status_code == 200 and r.json()["data"]["result"]["pending_approval"] is True
    assert world["calls"] == []
    # The person at their own dashboard runs it directly.
    _browser_admin(client)
    r = client.post(f"/api/connector-bindings/{world['binding']['id']}/run", headers=csrf_headers(client),
                    json={"action": "do_thing", "params": {"y": 3}})
    assert r.status_code == 200 and r.json()["data"]["result"]["success"] is True
    assert len(world["calls"]) == 1


def test_a_gate_can_hold_only_named_agents(world):
    _gate(world, actors=["runner"])
    assert _run(world, world["runner"]).json()["data"]["pending_approval"] is True
    assert _run(world, world["other"]).json()["data"]["success"] is True
    # A token is not an agent, so a gate that names agents does not hold it.
    token, _ = auth_service.create_operator_token("admin", 15)
    r = world["client"].post(f"/api/connector-bindings/{world['binding']['id']}/run", headers=_bearer(token),
                             json={"action": "do_thing", "params": {}})
    assert r.json()["data"]["result"]["success"] is True


def test_nobody_but_a_person_in_the_dashboard_can_change_or_remove_a_gate(world):
    client = world["client"]
    _gate(world)
    url = f"/api/connector-types/{SERVICE}/gates/do_thing"
    off = {"enabled": False, "actors": "all"}
    # An agent key, even one with every capability.
    assert client.put(url, headers=_bearer(world["runner"]), json=off).status_code in (401, 403)
    # An operator token (acts as the admin, but is not the dashboard).
    token, _ = auth_service.create_operator_token("admin", 15)
    assert client.put(url, headers=_bearer(token), json=off).status_code == 403
    # The admin's own session token sent as a Bearer key.
    session_id = auth_service.create_session("admin")["session_id"]
    assert client.put(url, headers=_bearer(session_id), json=off).status_code in (401, 403)
    # A regular user in the dashboard.
    auth_service.create_user("regular", "regular@test.local", "testpassword123", "Regular", "user")
    client.cookies.clear()
    client.cookies.set("session_token", auth_service.create_session("regular")["session_id"])
    assert client.put(url, headers=csrf_headers(client), json=off).status_code == 403
    client.cookies.clear()
    # None of that removed it.
    assert _run(world, world["runner"]).json()["data"]["pending_approval"] is True
    # The admin in the dashboard can, and it is audited with who and what changed.
    _gate(world, enabled=False)
    assert _run(world, world["runner"]).json()["data"]["success"] is True
    from app.database import get_db

    with get_db() as conn:
        events = conn.execute(
            "SELECT actor_id, details_json FROM audit_log WHERE action = 'connector_gate_changed' ORDER BY timestamp"
        ).fetchall()
    assert len(events) == 2 and {e["actor_id"] for e in events} == {"admin"}
    first = json.loads(events[0]["details_json"])
    assert first["action"] == "do_thing" and first["before"] is None and first["after"] == "all"


def test_turning_a_gate_off_does_not_release_a_waiting_request(world):
    _gate(world)
    approval_id = _run(world, world["runner"]).json()["data"]["approval_id"]
    _gate(world, enabled=False)
    from app.services import approval_service

    assert approval_service.get(approval_id)["status"] == "pending"
    assert world["calls"] == []


def test_an_agent_can_only_propose_a_gate_change_and_a_person_applies_it(world):
    client = world["client"]
    r = client.post("/api/approvals", headers=_bearer(world["runner"]), json={
        "scope": "workspace:ops", "title": "Hold do_thing for approval",
        "operations": [{"name": "connector_gate.set", "params": {
            "connector_type_id": SERVICE, "action": "do_thing", "enabled": True, "actors": "all"}}]})
    assert r.status_code == 201, r.text
    # Proposing changes nothing.
    assert _run(world, world["runner"]).json()["data"]["success"] is True
    approval_id = r.json()["data"]["approval"]["id"]
    assert _decide(world, approval_id).status_code == 200
    assert _run(world, world["runner"]).json()["data"]["pending_approval"] is True


def test_someone_who_can_write_in_the_workspace_may_decide_but_a_reader_may_not(world):
    from app.services import workspace_service

    _gate(world)
    approval_id = _run(world, world["runner"]).json()["data"]["approval_id"]
    client = world["client"]
    auth_service.create_user("reader", "reader@test.local", "testpassword123", "Reader", "user")
    auth_service.create_user("writer", "writer@test.local", "testpassword123", "Writer", "user")
    workspace_service.upsert_workspace_collaborator(workspace_id="ops", user_id="reader", can_read=True, can_write=False, created_by="admin")
    workspace_service.upsert_workspace_collaborator(workspace_id="ops", user_id="writer", can_read=True, can_write=True, created_by="admin")
    client.cookies.clear()
    client.cookies.set("session_token", auth_service.create_session("reader")["session_id"])
    assert client.post(f"/api/approvals/{approval_id}/decide", headers=csrf_headers(client),
                       json={"approve": True}).status_code == 403
    client.cookies.clear()
    client.cookies.set("session_token", auth_service.create_session("writer")["session_id"])
    assert client.post(f"/api/approvals/{approval_id}/decide", headers=csrf_headers(client),
                       json={"approve": False, "note": "no"}).status_code == 200


def test_an_agent_cannot_decide_its_own_request(world):
    _gate(world)
    approval_id = _run(world, world["runner"]).json()["data"]["approval_id"]
    r = world["client"].post(f"/api/approvals/{approval_id}/decide", headers=_bearer(world["runner"]), json={"approve": True})
    assert r.status_code == 403 and world["calls"] == []


def test_actions_list_says_which_actions_wait_for_a_person(world):
    _gate(world)
    r = _mcp(world["client"], world["runner"], "connectors_actions_list", {"connector_type_id": SERVICE})
    assert r.status_code == 200, r.text
    tools = {t.get("name") or t.get("action"): t for t in r.json()["data"].get("tools", r.json()["data"].get("actions", []))}
    assert any(t.get("needs_approval") for t in tools.values()), tools


def test_the_approval_page_says_it_in_plain_words_and_hides_anything_secret_looking(world):
    _gate(world)
    held = _run(world, world["runner"], params={"title": "hello", "api_token": "sk-live-123", "note": "Bearer abc"}).json()["data"]
    client = world["client"]
    _browser_admin(client)
    page = client.get(f"/approvals/{held['approval_id']}")
    assert page.status_code == 200
    html = page.text
    assert "runner wants to run do_thing on the Gate Service service" in html
    assert "It waits until" in html and "then it expires" in html
    assert "<code>title</code>" in html and "hello" in html
    assert "sk-live-123" not in html and "Bearer abc" not in html and html.count("[hidden]") == 2
    assert "Approve and run" in html and "data-approval-reject=" in html
    # A GET only shows the request; it never decides.
    from app.services import approval_service

    assert approval_service.get(held["approval_id"])["status"] == "pending"
    assert "Waiting for a decision" in client.get("/approvals").text
    assert "Run do_thing on Gate Service" in client.get("/approvals").text


def test_a_reader_sees_the_request_but_not_the_buttons(world):
    from app.services import workspace_service

    _gate(world)
    held = _run(world, world["runner"]).json()["data"]
    client = world["client"]
    auth_service.create_user("reader2", "reader2@test.local", "testpassword123", "Reader Two", "user")
    workspace_service.upsert_workspace_collaborator(workspace_id="ops", user_id="reader2", can_read=True, can_write=False, created_by="admin")
    client.cookies.clear()
    client.cookies.set("session_token", auth_service.create_session("reader2")["session_id"])
    html = client.get(f"/approvals/{held['approval_id']}").text
    assert "wants to run do_thing" in html and "data-approval-approve=" not in html


def test_a_leftover_trusted_device_session_cannot_decide_a_held_run(world):
    from app.services.auth_service import create_session

    _gate(world)
    approval_id = _run(world, world["runner"]).json()["data"]["approval_id"]
    client = world["client"]
    client.cookies.clear()
    client.cookies.set("session_token", create_session("admin", channel="approvals")["session_id"], path="/")
    r = client.post(f"/api/approvals/{approval_id}/decide", headers=csrf_headers(client), json={"approve": True})
    assert r.status_code == 401 and world["calls"] == []
