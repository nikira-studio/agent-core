"""
Integration tests for workspace-aware activity pickup.

Flow under test:
  1. Human creates an activity via dashboard REST with assigned_agent_id + memory_scope
  2. Agent calls POST /api/activity/pickup → receives the claimed activity
  3. Workspace isolation: an agent with different scopes cannot claim work meant for another
"""
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_agent(test_client, admin_token, agent_id, read_scopes=None, write_scopes=None):
    read_scopes = read_scopes or [f"agent:{agent_id}"]
    write_scopes = write_scopes or [f"agent:{agent_id}"]
    r = test_client.post(
        "/api/agents",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "id": agent_id,
            "display_name": agent_id,
            "description": "test",
            "read_scopes": read_scopes,
            "write_scopes": write_scopes,
        },
    )
    assert r.status_code in (200, 201), f"create agent {agent_id} failed: {r.json()}"


def _get_api_key(test_client, admin_token, agent_id):
    r = test_client.post(
        "/api/integrations/generate-connection",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"user_id": "admin", "agent_id": agent_id, "output_type": "env"},
    )
    assert r.status_code == 200, f"key gen failed: {r.json()}"
    return r.json()["data"]["api_key"]


def _assign_activity(test_client, admin_token, agent_id, memory_scope):
    r = test_client.post(
        "/api/activity",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "assigned_agent_id": agent_id,
            "task_description": f"Task for {agent_id}",
            "memory_scope": memory_scope,
        },
    )
    assert r.status_code == 201, f"create activity failed: {r.json()}"
    return r.json()["data"]["activity"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_pickup_requires_agent_auth(test_client, admin_token):
    r = test_client.post(
        "/api/activity/pickup",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "AGENT_REQUIRED"


def test_pickup_returns_empty_when_no_work(test_client, agent_token):
    r = test_client.post(
        "/api/activity/pickup",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["activity"] is None
    assert data["message"] is not None


def test_pickup_returns_assigned_work(test_client, admin_token, agent_token):
    act = _assign_activity(test_client, admin_token, "testagent", "agent:testagent")
    r = test_client.post(
        "/api/activity/pickup",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert r.status_code == 200
    claimed = r.json()["data"]["activity"]
    assert claimed is not None
    assert claimed["id"] == act["id"]
    assert claimed["assigned_agent_id"] == "testagent"


def test_pickup_scope_isolation_no_workspace_access(test_client, admin_token):
    _create_agent(
        test_client, admin_token, "agent-ws",
        read_scopes=["agent:agent-ws", "workspace:proj-ws"],
        write_scopes=["agent:agent-ws", "workspace:proj-ws"],
    )
    ws_key = _get_api_key(test_client, admin_token, "agent-ws")

    _create_agent(
        test_client, admin_token, "agent-nows",
        read_scopes=["agent:agent-nows"],
        write_scopes=["agent:agent-nows"],
    )
    nows_key = _get_api_key(test_client, admin_token, "agent-nows")

    # Create workspace-scoped task for agent-ws
    r = test_client.post(
        "/api/activity",
        headers={"Authorization": f"Bearer {ws_key}"},
        json={
            "task_description": "Workspace task",
            "memory_scope": "agent:agent-ws",
        },
    )
    assert r.status_code == 201

    # agent-nows has no access to agent:agent-ws scope, should get nothing
    r = test_client.post(
        "/api/activity/pickup",
        headers={"Authorization": f"Bearer {nows_key}"},
    )
    assert r.status_code == 200
    assert r.json()["data"]["activity"] is None


def test_different_assigned_agent_cannot_claim(test_client, admin_token):
    _create_agent(test_client, admin_token, "agent-alpha",
                  read_scopes=["agent:agent-alpha"],
                  write_scopes=["agent:agent-alpha"])
    _create_agent(test_client, admin_token, "agent-beta",
                  read_scopes=["agent:agent-beta"],
                  write_scopes=["agent:agent-beta"])
    alpha_key = _get_api_key(test_client, admin_token, "agent-alpha")
    beta_key = _get_api_key(test_client, admin_token, "agent-beta")

    _assign_activity(test_client, admin_token, "agent-alpha", "agent:agent-alpha")

    # agent-beta cannot claim agent-alpha's task
    r = test_client.post(
        "/api/activity/pickup",
        headers={"Authorization": f"Bearer {beta_key}"},
    )
    assert r.status_code == 200
    assert r.json()["data"]["activity"] is None

    # agent-alpha can claim its own task
    r = test_client.post(
        "/api/activity/pickup",
        headers={"Authorization": f"Bearer {alpha_key}"},
    )
    assert r.status_code == 200
    assert r.json()["data"]["activity"] is not None
    assert r.json()["data"]["activity"]["assigned_agent_id"] == "agent-alpha"


def test_pickup_does_not_return_completed_activity(test_client, admin_token, agent_token):
    act = _assign_activity(test_client, admin_token, "testagent", "agent:testagent")
    test_client.put(
        f"/api/activity/{act['id']}",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"status": "completed"},
    )
    r = test_client.post(
        "/api/activity/pickup",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert r.status_code == 200
    assert r.json()["data"]["activity"] is None


def test_pickup_audit_event_written(test_client, admin_token, agent_token):
    import json as _json
    _assign_activity(test_client, admin_token, "testagent", "agent:testagent")
    r = test_client.post(
        "/api/activity/pickup",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert r.status_code == 200
    claimed = r.json()["data"]["activity"]
    assert claimed is not None

    from app.database import get_db
    with get_db() as conn:
        row = conn.execute(
            "SELECT details_json FROM audit_log WHERE action = 'activity_pickup' AND resource_id = ? LIMIT 1",
            (claimed["id"],),
        ).fetchone()
    assert row is not None
    details = _json.loads(row["details_json"])
    assert details["action"] == "pickup"


def test_mcp_activity_pickup_returns_assigned_work(test_client, admin_token, agent_token):
    _assign_activity(test_client, admin_token, "testagent", "agent:testagent")
    r = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"tool": "activity_pickup", "params": {}},
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["activity"] is not None
    assert data["activity"]["assigned_agent_id"] == "testagent"


def test_mcp_activity_pickup_empty_when_no_work(test_client, agent_token):
    r = test_client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"tool": "activity_pickup", "params": {}},
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["activity"] is None
