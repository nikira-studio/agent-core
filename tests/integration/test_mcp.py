
import json


def test_mcp_manifest(test_client, agent_token):
    r = test_client.get("/mcp", headers={"Authorization": f"Bearer {agent_token}"})
    assert r.status_code == 200
    data = r.json()
    assert data["schema_version"] == "1.0"
    tool_names = {t["name"] for t in data["tools"]}
    expected = {
        "memory_search",
        "memory_get",
        "memory_write",
        "memory_retract",
        "credential_get",
        "credential_list",
        "activity_update",
        "activity_get",
        "activity_list",
        "connectors_list",
        "connectors_actions_list",
        "connectors_bindings_list",
        "connectors_bindings_test",
        "connectors_run",
        "get_briefing",
        "briefing_list",
    }
    assert expected.issubset(tool_names)


def test_mcp_jsonrpc_initialize(test_client, agent_token):
    r = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert data["jsonrpc"] == "2.0"
    assert data["id"] == 1
    assert data["result"]["serverInfo"]["name"] == "Agent Core"
    assert "tools" in data["result"]["capabilities"]


def test_mcp_jsonrpc_initialized_notification(test_client, agent_token):
    r = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    assert r.status_code == 202
    assert r.content == b""


def test_mcp_jsonrpc_tools_list(test_client, agent_token):
    r = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["jsonrpc"] == "2.0"
    assert data["id"] == 2
    tool_names = {t["name"] for t in data["result"]["tools"]}
    assert "connectors_run" in tool_names
    assert "connectors_list" in tool_names
    assert data["result"]["tools"][0]["inputSchema"]["type"] == "object"


def test_mcp_connector_tool_roundtrip(test_client, agent_token):
    calls = [
        ("connectors_list", {}),
        ("connectors_actions_list", {"connector_type_id": "generic_http"}),
        ("connectors_bindings_list", {}),
        ("connectors_bindings_test", {"binding_id": "missing"}),
    ]
    for idx, (tool, params) in enumerate(calls, start=10):
        r = test_client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {agent_token}"},
            json={"tool": tool, "params": params},
        )
        assert r.status_code in (200, 400, 404, 403)
        body = r.json()
        assert body["ok"] in (True, False)


def test_mcp_jsonrpc_tools_call(test_client, agent_token):
    r = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "memory_write",
                "arguments": {
                    "content": "JSON-RPC tool call test",
                    "memory_class": "scratchpad",
                    "scope": "agent:testagent",
                },
            },
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert data["jsonrpc"] == "2.0"
    assert data["id"] == 3
    assert data["result"]["isError"] is False
    assert data["result"]["content"][0]["type"] == "text"
    assert "JSON-RPC tool call test" in data["result"]["content"][0]["text"]


def test_mcp_tool_credential_list(test_client, agent_token):
    r = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"tool": "credential_list", "params": {}},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_mcp_tool_activity_create(test_client, agent_token):
    r = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "tool": "activity_update",
            "params": {"task_description": "MCP test activity"},
        },
    )
    assert r.status_code == 201
    assert r.json()["ok"] is True


def test_mcp_tool_activity_list(test_client, agent_token):
    created = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "tool": "activity_update",
            "params": {"task_description": "List activity test"},
        },
    )
    assert created.status_code == 201, created.json()

    r = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"tool": "activity_list", "params": {"limit": 10}},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert "activities" in data["data"]
    assert data["data"]["count"] >= 1


def test_mcp_tool_unknown(test_client, agent_token):
    r = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"tool": "nonexistent_tool", "params": {}},
    )
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_mcp_memory_write_rejects_removed_classes(test_client, agent_token):
    r = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "tool": "memory_write",
            "params": {
                "content": "This should not be accepted",
                "memory_class": "profile",
                "scope": "agent:testagent",
            },
        },
    )
    assert r.status_code == 400
    assert r.json()["ok"] is False
    assert r.json()["error"]["code"] == "INVALID_CLASS"


def test_mcp_memory_write_rejects_invalid_source_kind(test_client, agent_token):
    r = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "tool": "memory_write",
            "params": {
                "content": "This should not be accepted",
                "memory_class": "fact",
                "source_kind": "user_assertion",
                "scope": "agent:testagent",
            },
        },
    )
    assert r.status_code == 400
    assert r.json()["ok"] is False
    assert r.json()["error"]["code"] == "INVALID_SOURCE_KIND"


def test_mcp_activity_update_heartbeats_existing_activity(test_client, agent_token):
    from app.database import get_db

    created = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "tool": "activity_update",
            "params": {"task_description": "Heartbeat test"},
        },
    )
    assert created.status_code == 201, created.json()
    activity_id = created.json()["data"]["activity"]["id"]

    with get_db() as conn:
        conn.execute(
            "UPDATE agent_activity SET heartbeat_at = '2000-01-01T00:00:00', updated_at = '2000-01-01T00:00:00' WHERE id = ?",
            (activity_id,),
        )
        conn.commit()

    updated = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"tool": "activity_update", "params": {}},
    )
    assert updated.status_code == 200, updated.json()
    activity = updated.json()["data"]["activity"]
    assert activity["id"] == activity_id
    assert activity["heartbeat_at"] != "2000-01-01T00:00:00"


def test_mcp_activity_update_can_change_existing_task_and_scope(
    test_client, agent_token
):
    created = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "tool": "activity_update",
            "params": {
                "task_description": "Initial task",
                "memory_scope": "agent:testagent",
            },
        },
    )
    assert created.status_code == 201, created.json()
    activity_id = created.json()["data"]["activity"]["id"]

    updated = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "tool": "activity_update",
            "params": {
                "task_description": "Updated task",
                "memory_scope": "agent:testagent",
            },
        },
    )
    assert updated.status_code == 200, updated.json()
    activity = updated.json()["data"]["activity"]
    assert activity["id"] == activity_id
    assert activity["task_description"] == "Updated task"
    assert activity["memory_scope"] == "agent:testagent"


def test_mcp_tool_briefing_list(test_client, agent_token):
    created = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "tool": "activity_update",
            "params": {"task_description": "Briefing list test"},
        },
    )
    assert created.status_code == 201, created.json()
    activity_id = created.json()["data"]["activity"]["id"]

    briefing = test_client.post(
        "/api/briefings/handoff",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"activity_id": activity_id},
    )
    assert briefing.status_code == 201, briefing.json()

    r = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"tool": "briefing_list", "params": {"limit": 10}},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert "briefings" in data["data"]
    assert data["data"]["count"] >= 1


def test_mcp_memory_search_rejects_removed_classes(test_client, agent_token):
    r = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "tool": "memory_search",
            "params": {"query": "workspace", "memory_class": "opinion"},
        },
    )
    assert r.status_code == 400
    assert r.json()["ok"] is False
    assert r.json()["error"]["code"] == "INVALID_CLASS"


def test_mcp_tool_memory_write_supports_slot_key_and_freshness(
    test_client, agent_token
):
    r = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "tool": "memory_write",
            "params": {
                "content": "Use concise MCP answers",
                "memory_class": "preference",
                "scope": "agent:testagent",
                "slot_key": "style",
                "valid_from": "2026-05-15T00:00:00Z",
                "last_confirmed_at": "2026-05-15T01:00:00Z",
            },
        },
    )
    assert r.status_code == 201, r.json()
    record = r.json()["data"]["record"]
    assert record["slot_key"] == "style"
    assert record["valid_from"] == "2026-05-15T00:00:00+00:00"
    assert record["last_confirmed_at"] == "2026-05-15T01:00:00+00:00"


def test_mcp_tool_memory_write_ignores_client_supplied_provenance(
    test_client, agent_token
):
    r = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "tool": "memory_write",
            "params": {
                "content": "MCP provenance should be server stamped",
                "memory_class": "fact",
                "scope": "agent:testagent",
                "provenance": {
                    "actor_id": "spoofed",
                    "channel": "client",
                    "source_kind": "tool_output",
                },
            },
        },
    )
    assert r.status_code == 201, r.json()
    record = r.json()["data"]["record"]
    provenance = json.loads(record["provenance_json"])
    assert provenance["actor_id"] != "spoofed"
    assert provenance["channel"] == "mcp"
    assert provenance["route"] == "/mcp"
