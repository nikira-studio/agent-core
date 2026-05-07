import pytest


def test_create_agent(test_client, admin_token):
    r = test_client.post(
        "/api/agents",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"id": "testagent", "display_name": "Test Agent", "description": "Test agent"},
    )
    assert r.status_code == 201, f"create failed: {r.json()}"
    data = r.json()["data"]
    assert data["agent"]["id"] == "testagent"
    assert "api_key" not in data
    assert "next_step" in data


def test_non_admin_can_create_and_manage_owned_agent(test_client, admin_token):
    from app.services.auth_service import create_user, create_session

    create_user("owner", "owner@test.local", "testpassword123", "Owner", "user")
    owner_token = create_session("owner")["session_id"]

    r = test_client.post(
        "/api/agents",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={
            "id": "owneragent",
            "display_name": "Owner Agent",
            "read_scopes": ["user:owner"],
            "write_scopes": ["user:owner"],
        },
    )
    assert r.status_code == 201, r.json()
    assert r.json()["data"]["agent"]["owner_user_id"] == "owner"

    rotate = test_client.post(
        "/api/agents/owneragent/rotate_key",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert rotate.status_code == 200, rotate.json()
    assert rotate.json()["data"]["api_key"].startswith("ac_sk_")


def test_non_admin_cannot_grant_unowned_or_shared_write_scopes(test_client, admin_token):
    from app.services.auth_service import create_user, create_session
    from app.services.workspace_service import create_workspace

    create_user("owner", "owner@test.local", "testpassword123", "Owner", "user")
    create_user("other", "other@test.local", "testpassword123", "Other", "user")
    create_workspace("otherproject", "Other Project", "other")
    owner_token = create_session("owner")["session_id"]

    shared_write = test_client.post(
        "/api/agents",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={"id": "badshared", "display_name": "Bad Shared", "write_scopes": ["shared"]},
    )
    assert shared_write.status_code == 403

    unowned_project = test_client.post(
        "/api/agents",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={"id": "badworkspace", "display_name": "Bad Workspace", "read_scopes": ["workspace:otherworkspace"]},
    )
    assert unowned_project.status_code == 403


def test_admin_cannot_grant_other_users_personal_scope_to_agent(test_client, admin_token):
    from app.services.auth_service import create_user

    create_user("other", "other@test.local", "testpassword123", "Other", "user")

    create = test_client.post(
        "/api/agents",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"id": "crossuseragent", "display_name": "Cross User Agent", "read_scopes": ["user:other"]},
    )
    assert create.status_code == 403
    assert "owner" in create.json()["error"]["message"]


def test_rotate_agent_key(test_client, admin_token):
    r = test_client.post(
        "/api/agents",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"id": "rotateagent", "display_name": "Rotate Agent"},
    )
    assert r.status_code == 201
    assert "api_key" not in r.json()["data"]
    r2 = test_client.post(
        "/api/agents/rotateagent/rotate_key",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r2.status_code == 200
    new_key = r2.json()["data"]["api_key"]
    assert new_key.startswith("ac_sk_")


def test_deactivate_agent(test_client, admin_token):
    test_client.post(
        "/api/agents",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"id": "deactagent", "display_name": "Deact Agent"},
    )
    r = test_client.delete(
        "/api/agents/deactagent",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    r2 = test_client.get(
        "/api/agents/deactagent",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r2.json()["data"]["agent"]["is_active"] == False


def test_solo_mode_setting_controls_default_user_scope(test_client, admin_token):
    from app.database import get_db
    import json

    with get_db() as conn:
        conn.execute(
            "UPDATE system_settings SET value = 'false' WHERE key = 'solo_mode_enabled'"
        )
        conn.commit()

    r = test_client.post(
        "/api/agents",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"id": "nosoloagent", "display_name": "No Solo Agent"},
    )
    assert r.status_code == 201, r.json()
    read_scopes = json.loads(r.json()["data"]["agent"]["read_scopes_json"])
    assert "agent:nosoloagent" in read_scopes
    assert "shared" in read_scopes
    assert "user:admin" not in read_scopes


def test_agent_create_with_custom_scopes_preserves_own_scope(test_client, admin_token):
    import json

    r = test_client.post(
        "/api/agents",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "id": "customscopeagent",
            "display_name": "Custom Scope Agent",
            "read_scopes": ["shared"],
            "write_scopes": [],
        },
    )
    assert r.status_code == 201, r.json()
    agent = r.json()["data"]["agent"]
    assert "agent:customscopeagent" in json.loads(agent["read_scopes_json"])
    assert "agent:customscopeagent" in json.loads(agent["write_scopes_json"])


def test_agent_update_preserves_own_scope(test_client, admin_token):
    import json

    create = test_client.post(
        "/api/agents",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"id": "updatescopeagent", "display_name": "Update Scope Agent"},
    )
    assert create.status_code == 201, create.json()

    update = test_client.put(
        "/api/agents/updatescopeagent",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"read_scopes": ["shared"], "write_scopes": []},
    )
    assert update.status_code == 200, update.json()

    fetched = test_client.get(
        "/api/agents/updatescopeagent",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    agent = fetched.json()["data"]["agent"]
    assert "agent:updatescopeagent" in json.loads(agent["read_scopes_json"])
    assert "agent:updatescopeagent" in json.loads(agent["write_scopes_json"])


def test_agent_scopes_are_normalized_and_deduplicated(test_client, admin_token):
    import json

    create = test_client.post(
        "/api/agents",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "id": "DupeScopeAgent",
            "display_name": "Dupe Scope Agent",
            "read_scopes": ["agent:dupescopeagent", "agent:dupescopeagent", "shared"],
            "write_scopes": ["agent:dupescopeagent", "agent:dupescopeagent"],
        },
    )
    assert create.status_code == 201, create.json()

    agent = create.json()["data"]["agent"]
    read_scopes = json.loads(agent["read_scopes_json"])
    write_scopes = json.loads(agent["write_scopes_json"])

    assert read_scopes.count("agent:dupescopeagent") == 1
    assert write_scopes.count("agent:dupescopeagent") == 1
    assert read_scopes.count("shared") == 1


def test_agent_purge_removes_scoped_data_and_access_references(test_client, admin_token):
    import json
    from app.database import get_db
    from app.services import agent_service, auth_service, memory_service, vault_service, activity_service

    agent_service.create_agent(
        agent_id="purgeagent",
        display_name="Purge Agent",
        owner_user_id="admin",
    )
    agent_service.create_agent(
        agent_id="readeragent",
        display_name="Reader Agent",
        owner_user_id="admin",
        read_scopes=["agent:readeragent", "agent:purgeagent"],
        write_scopes=["agent:readeragent", "agent:purgeagent"],
    )
    memory_service.write_memory("agent scoped memory", "fact", "agent:purgeagent")
    vault_service.create_vault_entry("agent:purgeagent", "agent secret", "secret")
    activity_service.create_activity("purgeagent", "admin", "Purge task", "agent:purgeagent")
    fresh_admin_token = auth_service.create_session("admin")["session_id"]

    r = test_client.post(
        "/api/agents/purgeagent/purge",
        headers={"Authorization": f"Bearer {fresh_admin_token}"},
    )
    assert r.status_code == 200, r.json()

    with get_db() as conn:
        assert conn.execute("SELECT 1 FROM agents WHERE id = 'purgeagent'").fetchone() is None
        assert conn.execute("SELECT 1 FROM memory_records WHERE scope = 'agent:purgeagent'").fetchone() is None
        assert conn.execute("SELECT 1 FROM vault_entries WHERE scope = 'agent:purgeagent'").fetchone() is None
        assert conn.execute("SELECT 1 FROM agent_activity WHERE agent_id = 'purgeagent'").fetchone() is None
        reader = conn.execute(
            "SELECT read_scopes_json, write_scopes_json FROM agents WHERE id = 'readeragent'"
        ).fetchone()
    assert "agent:purgeagent" not in json.loads(reader["read_scopes_json"])
    assert "agent:purgeagent" not in json.loads(reader["write_scopes_json"])
