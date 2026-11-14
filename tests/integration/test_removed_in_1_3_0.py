"""Removed tools and routes answer old clients with what happened, not a 404."""


def _mcp(client, token, tool):
    return client.post("/mcp", headers={"Authorization": f"Bearer {token}"}, json={"tool": tool, "params": {}})


def test_removed_mcp_tools_answer_tool_removed(test_client, agent_token):
    for tool in ("memory_pin", "memory_pinned", "memory_feedback", "activity_pickup"):
        r = _mcp(test_client, agent_token, tool)
        assert r.status_code == 410, tool
        error = r.json()["error"]
        assert error["code"] == "TOOL_REMOVED" and "1.3.0" in error["message"], tool


def test_removed_tools_are_not_offered(test_client, agent_token):
    from app.routes.mcp import MANIFEST

    offered = {tool["name"] for tool in MANIFEST["tools"]}
    assert not offered & {"memory_pin", "memory_pinned", "memory_feedback", "activity_pickup"}


def test_removed_rest_routes_answer_removed(test_client, admin_token, agent_token):
    r = test_client.post("/api/activity/pickup", headers={"Authorization": f"Bearer {agent_token}"})
    assert r.status_code == 410 and r.json()["error"]["code"] == "REMOVED"
    r = test_client.post(
        "/api/workspace-sync/end", headers={"Authorization": f"Bearer {agent_token}"}, json={"execution_id": "x"}
    )
    assert r.status_code == 410 and r.json()["error"]["code"] == "REMOVED"


def test_an_old_client_sending_min_confidence_is_not_refused(test_client, agent_token):
    r = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"tool": "memory_search", "params": {"query": "anything", "min_confidence": 0.9}},
    )
    assert r.status_code == 200, r.text


def test_recovery_no_longer_offers_reassigning_to_another_agent(test_client, admin_token, agent_token):
    from app.services import activity_service

    task = activity_service.create_activity(agent_id="testagent", user_id="admin", task_description="T", memory_scope="agent:testagent")
    r = test_client.post(
        f"/api/activity/{task['id']}/recovery",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"action": "reassign_to_agent", "new_agent_id": "other"},
    )
    assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_ACTION"
