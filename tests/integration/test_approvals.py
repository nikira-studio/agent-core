"""Approvals: an agent proposes admin changes, a signed-in admin decides."""

from tests.conftest import csrf_headers


def _agent(client, admin_token, agent_id, capabilities):
    r = client.post("/api/agents", headers={"Authorization": f"Bearer {admin_token}"}, json={
        "id": agent_id, "display_name": agent_id, "read_scopes": ["workspace:ops"],
        "write_scopes": ["workspace:ops"], "capabilities": capabilities,
    })
    assert r.status_code in (200, 201), r.text
    k = client.post("/api/integrations/generate-connection", headers={"Authorization": f"Bearer {admin_token}"},
                    json={"user_id": "admin", "agent_id": agent_id, "output_type": "env"})
    assert k.status_code == 200, k.text
    return k.json()["data"]["api_key"]


def _workspace(client, admin_token):
    client.post("/api/workspaces", headers={"Authorization": f"Bearer {admin_token}"},
                json={"id": "ops", "name": "Ops"})


def _browser_admin(client):
    """Sign the test client in as the admin with a cookie session."""
    from app.services.auth_service import create_session

    client.cookies.clear()
    client.cookies.set("session_token", create_session("admin")["session_id"])


def _setup(test_client, admin_token):
    _workspace(test_client, admin_token)
    proposer = _agent(test_client, admin_token, "ops-agent", ["memory", "coordination", "admin_propose"])
    _agent(test_client, admin_token, "target-agent", ["memory"])
    return proposer


def _propose(client, key, operations, title="Widen target-agent"):
    return client.post("/api/approvals", headers={"Authorization": f"Bearer {key}"},
                       json={"scope": "workspace:ops", "title": title, "operations": operations})


def _update_target(description):
    return {"name": "agent.update", "params": {
        "agent_id": "target-agent", "display_name": "target-agent", "description": description,
        "read_scopes": ["workspace:ops"], "write_scopes": ["workspace:ops"], "capabilities": ["memory"],
    }}


def test_proposal_runs_only_after_a_person_approves(test_client, admin_token):
    key = _setup(test_client, admin_token)
    r = _propose(test_client, key, [_update_target("approved change")])
    assert r.status_code == 201, r.text
    approval_id = r.json()["data"]["approval"]["id"]

    from app.services import agent_service
    assert agent_service.get_agent_by_id("target-agent")["description"] != "approved change"

    # The proposing agent sees it pending and cannot decide it.
    seen = test_client.get(f"/api/approvals/{approval_id}", headers={"Authorization": f"Bearer {key}"})
    assert seen.json()["data"]["approval"]["status"] == "pending"
    assert test_client.post(f"/api/approvals/{approval_id}/decide", json={"approve": True},
                            headers={"Authorization": f"Bearer {key}"}).status_code == 403
    # Nor can an operator token.
    assert test_client.post(f"/api/approvals/{approval_id}/decide", json={"approve": True},
                            headers={"Authorization": f"Bearer {admin_token}"}).status_code == 403

    _browser_admin(test_client)
    assert test_client.post(f"/api/approvals/{approval_id}/decide", json={"approve": True}).status_code == 403  # no CSRF
    r = test_client.post(f"/api/approvals/{approval_id}/decide", json={"approve": True, "note": "ok"},
                         headers=csrf_headers(test_client))
    assert r.status_code == 200, r.text
    assert r.json()["data"]["approval"]["status"] == "done"
    assert agent_service.get_agent_by_id("target-agent")["description"] == "approved change"

    # Decided once only.
    again = test_client.post(f"/api/approvals/{approval_id}/decide", json={"approve": True},
                             headers=csrf_headers(test_client))
    assert again.status_code == 409


def test_a_failed_operation_undoes_the_whole_bundle(test_client, admin_token):
    key = _setup(test_client, admin_token)
    bad = {"name": "agent.update", "params": {"agent_id": "no-such-agent", "display_name": "x",
                                               "read_scopes": [], "write_scopes": [], "capabilities": ["memory"]}}
    approval_id = _propose(test_client, key, [_update_target("should not stick"), bad]).json()["data"]["approval"]["id"]
    _browser_admin(test_client)
    r = test_client.post(f"/api/approvals/{approval_id}/decide", json={"approve": True}, headers=csrf_headers(test_client))
    approval = r.json()["data"]["approval"]
    assert approval["status"] == "failed" and "Nothing was applied" in approval["error"]
    from app.services import agent_service
    assert agent_service.get_agent_by_id("target-agent")["description"] != "should not stick"


def test_a_target_changed_after_proposing_fails(test_client, admin_token):
    key = _setup(test_client, admin_token)
    approval_id = _propose(test_client, key, [_update_target("stale")]).json()["data"]["approval"]["id"]
    from app.services import agent_service
    agent_service.update_agent("target-agent", description="changed meanwhile")
    _browser_admin(test_client)
    r = test_client.post(f"/api/approvals/{approval_id}/decide", json={"approve": True}, headers=csrf_headers(test_client))
    assert r.json()["data"]["approval"]["status"] == "failed"
    assert agent_service.get_agent_by_id("target-agent")["description"] == "changed meanwhile"


def test_reject_records_the_note(test_client, admin_token):
    key = _setup(test_client, admin_token)
    approval_id = _propose(test_client, key, [_update_target("no")]).json()["data"]["approval"]["id"]
    _browser_admin(test_client)
    r = test_client.post(f"/api/approvals/{approval_id}/decide", json={"approve": False, "note": "use a read-only key"},
                         headers=csrf_headers(test_client))
    assert r.json()["data"]["approval"]["status"] == "rejected"
    seen = test_client.get(f"/api/approvals/{approval_id}", headers={"Authorization": f"Bearer {key}"})
    assert seen.json()["data"]["approval"]["decision_note"] == "use a read-only key"


def test_proposing_needs_the_capability_and_a_known_operation(test_client, admin_token):
    _workspace(test_client, admin_token)
    plain = _agent(test_client, admin_token, "plain-agent", ["memory"])
    assert _propose(test_client, plain, [_update_target("x")]).status_code == 403
    key = _agent(test_client, admin_token, "ops-agent", ["memory", "admin_propose"])
    assert _propose(test_client, key, [{"name": "user.delete", "params": {}}]).status_code == 400


def test_agents_never_supply_secret_values(test_client, admin_token):
    key = _setup(test_client, admin_token)
    r = _propose(test_client, key, [{"name": "credential.create", "params": {
        "scope": "workspace:ops", "name": "API_TOKEN", "value": "from-the-agent"}}])
    assert r.status_code == 400 and r.json()["error"]["code"] == "PERSON_FIELD"

    r = _propose(test_client, key, [{"name": "credential.create", "params": {"scope": "workspace:ops", "name": "API_TOKEN"}}])
    assert r.status_code == 201, r.text
    approval_id = r.json()["data"]["approval"]["id"]
    _browser_admin(test_client)
    r = test_client.post(f"/api/approvals/{approval_id}/decide", headers=csrf_headers(test_client),
                         json={"approve": True, "values": {"0.value": "typed-by-the-person"}})
    assert r.json()["data"]["approval"]["status"] == "done", r.text
    result = str(r.json()["data"]["approval"].get("result"))
    assert "typed-by-the-person" not in result


def test_the_mcp_tools_propose_a_change_and_report_its_status(test_client, admin_token):
    """An agent proposes over MCP and can watch what the person decided."""
    key = _setup(test_client, admin_token)
    r = test_client.post("/mcp", headers={"Authorization": f"Bearer {key}"}, json={
        "tool": "approval_propose",
        "params": {
            "scope": "workspace:ops",
            "title": "Widen target-agent",
            "operations": [_update_target("approved change")],
        },
    })
    assert r.status_code == 200, r.text
    approval_id = r.json()["data"]["approval"]["id"]
    assert approval_id

    seen = test_client.post("/mcp", headers={"Authorization": f"Bearer {key}"}, json={
        "tool": "approval_get",
        "params": {"approval_id": approval_id},
    })
    assert seen.status_code == 200, seen.text
    approval = seen.json()["data"]["approval"]
    assert approval["id"] == approval_id
    assert approval["status"] == "pending"

    _browser_admin(test_client)
    decided = test_client.post(f"/api/approvals/{approval_id}/decide",
                               json={"approve": False, "note": "not yet"},
                               headers=csrf_headers(test_client))
    assert decided.status_code == 200, decided.text

    seen = test_client.post("/mcp", headers={"Authorization": f"Bearer {key}"}, json={
        "tool": "approval_get",
        "params": {"approval_id": approval_id},
    })
    assert seen.json()["data"]["approval"]["status"] == "rejected"
    assert seen.json()["data"]["approval"]["decision_note"] == "not yet"


def test_a_pending_approval_is_on_the_attention_list(test_client, admin_token):
    """The queue of things waiting on a person carries the pending approvals."""
    key = _setup(test_client, admin_token)
    approval_id = _propose(test_client, key, [_update_target("approved change")]).json()["data"]["approval"]["id"]

    r = test_client.get("/api/activity/attention", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200, r.text
    approvals = r.json()["data"]["approvals"]
    assert approval_id in [a["id"] for a in approvals]



def test_an_agent_cannot_flood_the_queue(test_client, admin_token):
    key = _setup(test_client, admin_token)
    for n in range(10):
        assert _propose(test_client, key, [_update_target(f"change {n}")], title=f"t{n}").status_code == 201
    r = _propose(test_client, key, [_update_target("one too many")], title="t10")
    assert r.status_code == 429 and r.json()["error"]["code"] == "TOO_MANY_PENDING"
    # An identical proposal is still the same one, not a new slot.
    assert _propose(test_client, key, [_update_target("change 0")], title="t0").status_code == 201


def test_status_and_effects_commit_together(test_client, admin_token, monkeypatch):
    """If recording 'done' fails, the change it records is undone with it."""
    from app.security.exceptions import APIError
    from app.services import agent_service, approval_service

    key = _setup(test_client, admin_token)
    approval_id = _propose(test_client, key, [_update_target("must not stick")]).json()["data"]["approval"]["id"]

    def broken(_results):
        raise APIError("BOOM", "recording the result failed", 500)

    monkeypatch.setattr(approval_service, "_redact", broken)
    _browser_admin(test_client)
    r = test_client.post(f"/api/approvals/{approval_id}/decide", json={"approve": True}, headers=csrf_headers(test_client))
    assert r.json()["data"]["approval"]["status"] == "failed"
    assert agent_service.get_agent_by_id("target-agent")["description"] != "must not stick"
