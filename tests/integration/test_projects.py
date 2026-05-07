import pytest


def test_project_create(test_client, admin_token):
    r = test_client.post(
        "/api/workspaces",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"id": "test-workspace", "name": "Test Workspace"},
    )
    assert r.status_code == 201, f"create failed: {r.json()}"
    assert r.json()["data"]["workspace"]["id"] == "test-workspace"


def test_project_list(test_client, admin_token):
    test_client.post(
        "/api/workspaces",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"id": "list-test-workspace", "name": "List Test"},
    )
    r = test_client.get(
        "/api/workspaces",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    assert len(r.json()["data"]["workspaces"]) >= 1


def test_project_get(test_client, admin_token):
    test_client.post(
        "/api/workspaces",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"id": "get-test-workspace", "name": "Get Test"},
    )
    r = test_client.get(
        "/api/workspaces/get-test-workspace",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    assert r.json()["data"]["workspace"]["id"] == "get-test-workspace"


def test_project_deactivate(test_client, admin_token):
    test_client.post(
        "/api/workspaces",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"id": "deact-test-workspace", "name": "Deactivate Test"},
    )
    r = test_client.delete(
        "/api/workspaces/deact-test-workspace",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200


def test_non_admin_can_manage_owned_project(test_client, admin_token):
    from app.services.auth_service import create_user, create_session

    create_user("owner", "owner@test.local", "testpassword123", "Owner", "user")
    owner_token = create_session("owner")["session_id"]
    create = test_client.post(
        "/api/workspaces",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={"id": "owned-workspace", "name": "Owned Workspace"},
    )
    assert create.status_code == 201, create.json()

    update = test_client.put(
        "/api/workspaces/owned-workspace",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={"name": "Updated Owned Workspace"},
    )
    assert update.status_code == 200, update.json()

    deactivate = test_client.delete(
        "/api/workspaces/owned-workspace",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert deactivate.status_code == 200, deactivate.json()


def test_non_admin_cannot_manage_unowned_project(test_client, admin_token):
    from app.services.auth_service import create_user, create_session
    from app.services.workspace_service import create_workspace

    create_user("owner", "owner@test.local", "testpassword123", "Owner", "user")
    create_user("other", "other@test.local", "testpassword123", "Other", "user")
    create_workspace("other-workspace", "Other Workspace", "other")
    owner_token = create_session("owner")["session_id"]

    update = test_client.put(
        "/api/workspaces/other-workspace",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={"name": "Nope"},
    )
    assert update.status_code == 403


def test_project_purge_removes_scoped_data_and_access_references(test_client, admin_token):
    import json
    from app.database import get_db
    from app.services import agent_service, auth_service, memory_service, vault_service, workspace_service

    workspace_service.create_workspace("purge-workspace", "Purge Workspace", "admin")
    agent_service.create_agent(
        agent_id="projectreader",
        display_name="Workspace Reader",
        owner_user_id="admin",
        read_scopes=["agent:projectreader", "workspace:purge-workspace"],
        write_scopes=["agent:projectreader", "workspace:purge-workspace"],
    )
    memory_service.write_memory("workspace scoped memory", "fact", "workspace:purge-workspace")
    vault_service.create_vault_entry("workspace:purge-workspace", "workspace secret", "secret")
    fresh_admin_token = auth_service.create_session("admin")["session_id"]

    r = test_client.post(
        "/api/workspaces/purge-workspace/purge",
        headers={"Authorization": f"Bearer {fresh_admin_token}"},
    )
    assert r.status_code == 200, r.json()

    with get_db() as conn:
        assert conn.execute("SELECT 1 FROM workspaces WHERE id = 'purge-workspace'").fetchone() is None
        assert conn.execute("SELECT 1 FROM memory_records WHERE scope = 'workspace:purge-workspace'").fetchone() is None
        assert conn.execute("SELECT 1 FROM vault_entries WHERE scope = 'workspace:purge-workspace'").fetchone() is None
        agent = conn.execute(
            "SELECT read_scopes_json, write_scopes_json FROM agents WHERE id = 'projectreader'"
        ).fetchone()
    assert "workspace:purge-workspace" not in json.loads(agent["read_scopes_json"])
    assert "workspace:purge-workspace" not in json.loads(agent["write_scopes_json"])
