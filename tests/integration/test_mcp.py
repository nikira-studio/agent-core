import pytest


def test_mcp_manifest(test_client, agent_token):
    r = test_client.get("/mcp", headers={"Authorization": f"Bearer {agent_token}"})
    assert r.status_code == 200
    data = r.json()
    assert data["schema_version"] == "1.0"
    assert len(data["tools"]) == 9


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
    assert len(data["result"]["tools"]) == 9
    assert data["result"]["tools"][0]["inputSchema"]["type"] == "object"


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


def test_mcp_tool_vault_list(test_client, agent_token):
    r = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"tool": "vault_list", "params": {}},
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


def test_mcp_activity_update_can_change_existing_task_and_scope(test_client, agent_token):
    created = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "tool": "activity_update",
            "params": {"task_description": "Initial task", "memory_scope": "agent:testagent"},
        },
    )
    assert created.status_code == 201, created.json()
    activity_id = created.json()["data"]["activity"]["id"]

    updated = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={
            "tool": "activity_update",
            "params": {"task_description": "Updated task", "memory_scope": "agent:testagent"},
        },
    )
    assert updated.status_code == 200, updated.json()
    activity = updated.json()["data"]["activity"]
    assert activity["id"] == activity_id
    assert activity["task_description"] == "Updated task"
    assert activity["memory_scope"] == "agent:testagent"


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
