import json

from app.security.scope_enforcer import build_agent_context
from app.services.auth_service import create_session, create_user
from app.services.workspace_service import (
    can_user_read_workspace,
    can_user_write_workspace,
    get_workspace_by_id,
    list_workspace_collaborators,
    remove_workspace_collaborator,
    upsert_workspace_collaborator,
)
from app.services import agent_service


def test_workspace_collaborator_can_view_and_create_agent_scopes(test_client, admin_token):
    create_user("owner", "owner@test.local", "testpassword123", "Owner", "user")
    create_user("collab", "collab@test.local", "testpassword123", "Collab", "user")
    owner_token = create_session("owner")["session_id"]
    collab_token = create_session("collab")["session_id"]

    created = test_client.post(
        "/api/workspaces",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={
            "id": "sharedproject",
            "name": "Shared Project",
            "description": "Shared collaboration workspace",
        },
    )
    assert created.status_code == 201, created.json()

    grant = test_client.put(
        "/api/workspaces/sharedproject/collaborators/collab",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={"can_read": True, "can_write": True},
    )
    assert grant.status_code == 200, grant.json()

    fetched = test_client.get(
        "/api/workspaces/sharedproject",
        headers={"Authorization": f"Bearer {collab_token}"},
    )
    assert fetched.status_code == 200, fetched.json()

    listed = test_client.get(
        "/api/workspaces",
        headers={"Authorization": f"Bearer {collab_token}"},
    )
    assert listed.status_code == 200, listed.json()
    workspace_ids = {w["id"] for w in listed.json()["data"]["workspaces"]}
    assert "sharedproject" in workspace_ids

    created_agent = test_client.post(
        "/api/agents",
        headers={"Authorization": f"Bearer {collab_token}"},
        json={
            "id": "collabagent",
            "display_name": "Collab Agent",
            "read_scopes": ["workspace:sharedproject"],
            "write_scopes": ["workspace:sharedproject"],
        },
    )
    assert created_agent.status_code == 201, created_agent.json()

    agent = created_agent.json()["data"]["agent"]
    assert "workspace:sharedproject" in json.loads(agent["read_scopes_json"])
    assert "workspace:sharedproject" in json.loads(agent["write_scopes_json"])


def test_workspace_collaborator_revocation_blocks_agent_runtime_access(test_client, admin_token):
    create_user("owner2", "owner2@test.local", "testpassword123", "Owner2", "user")
    create_user("collab2", "collab2@test.local", "testpassword123", "Collab2", "user")
    owner_token = create_session("owner2")["session_id"]

    created = test_client.post(
        "/api/workspaces",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={
            "id": "revocationproject",
            "name": "Revocation Project",
        },
    )
    assert created.status_code == 201, created.json()

    grant = test_client.put(
        "/api/workspaces/revocationproject/collaborators/collab2",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={"can_read": True, "can_write": True},
    )
    assert grant.status_code == 200, grant.json()

    collab_token = create_session("collab2")["session_id"]
    created_agent = test_client.post(
        "/api/agents",
        headers={"Authorization": f"Bearer {collab_token}"},
        json={
            "id": "revocationagent",
            "display_name": "Revocation Agent",
            "read_scopes": ["workspace:revocationproject"],
            "write_scopes": ["workspace:revocationproject"],
        },
    )
    assert created_agent.status_code == 201, created_agent.json()

    agent = agent_service.get_agent_by_id("revocationagent")
    ctx = build_agent_context(agent)
    assert "revocationproject" in ctx.active_workspace_ids
    assert can_user_read_workspace("collab2", "revocationproject")
    assert can_user_write_workspace("collab2", "revocationproject")

    removed = remove_workspace_collaborator("revocationproject", "collab2")
    assert removed is True

    refreshed_agent = agent_service.get_agent_by_id("revocationagent")
    refreshed_ctx = build_agent_context(refreshed_agent)
    assert "revocationproject" not in refreshed_ctx.active_workspace_ids
    assert not can_user_read_workspace("collab2", "revocationproject")
    assert not can_user_write_workspace("collab2", "revocationproject")


def test_agent_workspace_authority_tracks_collaborator_permission_bits(test_client, admin_token):
    """An unchanged collaborator row must not keep an agent's old authority."""
    create_user("ownerbits", "ownerbits@test.local", "testpassword123", "Owner", "user")
    create_user("memberbits", "memberbits@test.local", "testpassword123", "Member", "user")
    owner_token = create_session("ownerbits")["session_id"]
    test_client.post(
        "/api/workspaces",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={"id": "permissionbits", "name": "Permission Bits"},
    )
    upsert_workspace_collaborator("permissionbits", "memberbits", True, True, "ownerbits")
    _, agent_key = agent_service.create_agent(
        "memberbitsagent", "Member agent", "memberbits",
        read_scopes=["workspace:permissionbits"],
        write_scopes=["workspace:permissionbits"],
    )
    scope = "workspace:permissionbits"

    context = build_agent_context(agent_service.get_agent_by_id("memberbitsagent"))
    assert scope in context.read_scopes
    assert scope in context.write_scopes

    # Retaining read access must not retain write access. Both transports use
    # the freshly-built runtime context rather than the agent's stored scopes.
    upsert_workspace_collaborator("permissionbits", "memberbits", True, False, "ownerbits")
    context = build_agent_context(agent_service.get_agent_by_id("memberbitsagent"))
    assert scope in context.read_scopes
    assert scope not in context.write_scopes
    payload = {"content": "must not write", "memory_class": "fact", "scope": scope}
    rest = test_client.post("/api/memory/write", headers={"Authorization": f"Bearer {agent_key}"}, json=payload)
    assert rest.status_code == 403, rest.json()
    mcp = test_client.post("/mcp", headers={"Authorization": f"Bearer {agent_key}"}, json={"tool": "memory_write", "params": payload})
    assert mcp.status_code == 403, mcp.json()

    # Read removal must likewise take effect without editing or disabling the
    # agent record itself.
    upsert_workspace_collaborator("permissionbits", "memberbits", False, False, "ownerbits")
    context = build_agent_context(agent_service.get_agent_by_id("memberbitsagent"))
    assert scope not in context.read_scopes
    assert scope not in context.write_scopes
    rest = test_client.post("/api/memory/search", headers={"Authorization": f"Bearer {agent_key}"}, json={"query": "anything", "scope": scope})
    assert rest.status_code == 403, rest.json()
    mcp = test_client.post("/mcp", headers={"Authorization": f"Bearer {agent_key}"}, json={"tool": "memory_search", "params": {"query": "anything", "scope": scope}})
    assert mcp.status_code == 403, mcp.json()


def test_disabled_default_user_cannot_lend_workspace_authority(test_client, admin_token):
    create_user("ownerprincipal", "ownerprincipal@test.local", "testpassword123", "Owner", "user")
    create_user("principal", "principal@test.local", "testpassword123", "Principal", "user")
    owner_token = create_session("ownerprincipal")["session_id"]
    test_client.post(
        "/api/workspaces",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={"id": "principalspace", "name": "Principal Space"},
    )
    upsert_workspace_collaborator("principalspace", "principal", True, True, "ownerprincipal")
    agent, _ = agent_service.create_agent(
        "principalagent", "Principal agent", "ownerprincipal", default_user_id="principal",
        read_scopes=["user:principal", "workspace:principalspace"],
        write_scopes=["workspace:principalspace"],
    )
    assert "workspace:principalspace" in build_agent_context(agent).read_scopes

    test_client.put(
        "/api/auth/users/principal",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"is_active": False},
    )
    context = build_agent_context(agent_service.get_agent_by_id("principalagent"))
    assert "user:principal" not in context.read_scopes
    assert "workspace:principalspace" not in context.read_scopes
    assert "workspace:principalspace" not in context.write_scopes

def test_non_owner_collaborator_cannot_manage_collaborators(test_client, admin_token):
    create_user("owner4", "owner4@test.local", "testpassword123", "Owner4", "user")
    create_user("collab4", "collab4@test.local", "testpassword123", "Collab4", "user")
    owner_token = create_session("owner4")["session_id"]
    collab_token = create_session("collab4")["session_id"]

    test_client.post(
        "/api/workspaces",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={"id": "managedproject", "name": "Managed Project"},
    )
    test_client.put(
        "/api/workspaces/managedproject/collaborators/collab4",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={"can_read": True, "can_write": False},
    )

    # collaborator can view the workspace
    r = test_client.get(
        "/api/workspaces/managedproject",
        headers={"Authorization": f"Bearer {collab_token}"},
    )
    assert r.status_code == 200

    # collaborator cannot list collaborators (triggers the UI 403 branch)
    r = test_client.get(
        "/api/workspaces/managedproject/collaborators",
        headers={"Authorization": f"Bearer {collab_token}"},
    )
    assert r.status_code == 403

    # collaborator cannot add another collaborator
    r = test_client.put(
        "/api/workspaces/managedproject/collaborators/someuser",
        headers={"Authorization": f"Bearer {collab_token}"},
        json={"can_read": True, "can_write": False},
    )
    assert r.status_code == 403

    # collaborator cannot remove a collaborator
    r = test_client.delete(
        "/api/workspaces/managedproject/collaborators/collab4",
        headers={"Authorization": f"Bearer {collab_token}"},
    )
    assert r.status_code == 403


def test_workspace_collaborator_listing_includes_owner_row(test_client, admin_token):
    create_user("owner3", "owner3@test.local", "testpassword123", "Owner3", "user")
    owner_token = create_session("owner3")["session_id"]

    created = test_client.post(
        "/api/workspaces",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={
            "id": "ownerproject",
            "name": "Owner Project",
        },
    )
    assert created.status_code == 201, created.json()

    workspace = get_workspace_by_id("ownerproject")
    assert workspace["owner_user_id"] == "owner3"

    rows = list_workspace_collaborators("ownerproject")
    assert any(row["user_id"] == "owner3" and row["role"] == "owner" for row in rows)
