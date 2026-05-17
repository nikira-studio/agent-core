import json

from app.security.scope_enforcer import build_agent_context
from app.services.auth_service import create_session, create_user
from app.services.workspace_service import (
    can_user_read_workspace,
    can_user_write_workspace,
    get_workspace_by_id,
    list_workspace_collaborators,
    remove_workspace_collaborator,
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
