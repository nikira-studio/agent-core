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
from tests.conftest import operator_token_for


def test_workspace_collaborator_can_view_and_create_agent_scopes(test_client, admin_token):
    create_user("owner", "owner@test.local", "testpassword123", "Owner", "user")
    create_user("collab", "collab@test.local", "testpassword123", "Collab", "user")
    owner_token = operator_token_for("owner")
    collab_token = operator_token_for("collab")

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
    owner_token = operator_token_for("owner2")

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

    collab_token = operator_token_for("collab2")
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
    owner_token = operator_token_for("ownerbits")
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
    owner_token = operator_token_for("ownerprincipal")
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

    # /api/auth/* is checked with a login session token, so it uses one here.
    admin_session = create_session("admin")["session_id"]
    test_client.put(
        "/api/auth/users/principal",
        headers={"Authorization": f"Bearer {admin_session}"},
        json={"is_active": False},
    )
    context = build_agent_context(agent_service.get_agent_by_id("principalagent"))
    assert "user:principal" not in context.read_scopes
    assert "workspace:principalspace" not in context.read_scopes
    assert "workspace:principalspace" not in context.write_scopes

def test_non_owner_collaborator_cannot_manage_collaborators(test_client, admin_token):
    create_user("owner4", "owner4@test.local", "testpassword123", "Owner4", "user")
    create_user("collab4", "collab4@test.local", "testpassword123", "Collab4", "user")
    owner_token = operator_token_for("owner4")
    collab_token = operator_token_for("collab4")

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
    owner_token = operator_token_for("owner3")

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


def test_admin_links_a_repository_folder_and_owners_cannot(test_client, admin_token):
    from app.services import session_context_service

    create_user("owner2", "owner2@test.local", "testpassword123", "Owner", "user")
    owner = {"Authorization": f"Bearer {operator_token_for('owner2')}"}
    admin = {"Authorization": f"Bearer {admin_token}"}
    assert test_client.post("/api/workspaces", headers=owner, json={"id": "linked", "name": "Linked"}).status_code == 201

    denied = test_client.put("/api/workspaces/linked", headers=owner, json={"repo_root": "/srv/x"})
    assert denied.status_code == 403
    relative = test_client.put("/api/workspaces/linked", headers=admin, json={"repo_root": "srv/x"})
    assert relative.status_code == 400

    assert test_client.put("/api/workspaces/linked", headers=admin, json={"repo_root": "/srv/x/"}).status_code == 200
    fetched = test_client.get("/api/workspaces/linked", headers=owner).json()["data"]["workspace"]
    assert fetched["repo_root"] == "/srv/x"
    # The owner can still save other fields with the unchanged folder.
    assert test_client.put("/api/workspaces/linked", headers=owner,
                           json={"name": "Renamed", "repo_root": "/srv/x"}).status_code == 200
    scope, _ = session_context_service.resolve_scope(["workspace:linked", "workspace:other"], None, "/srv/x/app")
    assert scope == "workspace:linked"

    assert test_client.put("/api/workspaces/linked", headers=admin, json={"repo_root": ""}).status_code == 200
    assert test_client.get("/api/workspaces/linked", headers=owner).json()["data"]["workspace"]["repo_root"] is None


def test_workspaces_page_script_does_not_call_server_helpers(test_client, admin_token):
    # The dashboard page is read with the browser's session cookie, so it needs
    # a real login session (an operator token is a Bearer key, not a session).
    test_client.cookies.set("session_token", create_session("admin")["session_id"])
    page = test_client.get("/workspaces").text
    test_client.cookies.clear()
    assert "get_icon(" not in page
    assert 'id="ep-repo-roots"' in page and "Add folder" in page and "one per machine where this project lives" in page.lower()
    assert "function addRepoRootRow" in page and "repo_roots: repoRootsValue()" in page


def test_a_windows_repository_folder_links_and_matches_a_windows_session(test_client, admin_token):
    from app.services import session_context_service

    admin = {"Authorization": f"Bearer {admin_token}"}
    assert test_client.post("/api/workspaces", headers=admin, json={"id": "winrepo", "name": "Win"}).status_code == 201
    saved = test_client.put("/api/workspaces/winrepo", headers=admin, json={"repo_root": "c:/Users/Alex/media studio/"})
    assert saved.status_code == 200
    fetched = test_client.get("/api/workspaces/winrepo", headers=admin).json()["data"]["workspace"]
    assert fetched["repo_root"] == "C:\\Users\\Alex\\media studio"
    scopes = ["workspace:winrepo", "workspace:other"]
    for cwd in ("C:\\Users\\Alex\\media studio", "c:\\users\\alex\\Media Studio\\clips"):
        scope, how = session_context_service.resolve_scope(scopes, None, cwd)
        assert (scope, how) == ("workspace:winrepo", "cwd")
    scope, how = session_context_service.resolve_scope(scopes, None, "C:\\Users\\Alex\\media studio-old")
    assert scope is None and how == "unmatched"
    assert test_client.put("/api/workspaces/winrepo", headers=admin, json={"repo_root": "C:repo"}).status_code == 400


def _put(client, headers, wid, **body):
    return client.put(f"/api/workspaces/{wid}", headers=headers, json=body)


def test_a_workspace_holds_several_folders_and_the_deepest_match_wins(test_client, admin_token):
    from app.services import session_context_service, workspace_service

    admin = {"Authorization": f"Bearer {admin_token}"}
    for wid in ("multi", "inner"):
        assert test_client.post("/api/workspaces", headers=admin, json={"id": wid, "name": wid}).status_code == 201
    saved = _put(test_client, admin, "multi", repo_roots=["/mnt/nas/proj", "D:/windows/Proj/", "/mnt/nas/proj", ""])
    assert saved.status_code == 200
    got = test_client.get("/api/workspaces/multi", headers=admin).json()["data"]["workspace"]
    assert got["repo_roots"] == ["/mnt/nas/proj", "D:\\windows\\Proj"] and got["repo_root"] == "/mnt/nas/proj"
    scopes = ["workspace:multi", "workspace:inner", "workspace:other"]
    for cwd in ("/mnt/nas/proj/src", "d:\\WINDOWS\\proj\\src"):
        assert session_context_service.resolve_scope(scopes, None, cwd) == ("workspace:multi", "cwd")
    # A folder inside one of them that another workspace claims more deeply goes to the deeper one.
    assert _put(test_client, admin, "inner", repo_roots=["D:\\windows\\Proj\\vendor", "/srv/other"]).status_code == 200
    assert session_context_service.resolve_scope(scopes, None, "D:\\windows\\Proj\\vendor\\lib")[0] == "workspace:inner"
    assert session_context_service.resolve_scope(scopes, None, "D:\\windows\\Proj\\src")[0] == "workspace:multi"
    # Remove one folder: the other still matches, the removed one does not.
    assert _put(test_client, admin, "multi", repo_roots=["D:\\windows\\Proj"]).status_code == 200
    assert workspace_service.get_repo_roots("multi") == ["D:\\windows\\Proj"]
    assert session_context_service.resolve_scope(scopes, None, "/mnt/nas/proj/src")[1] == "unmatched"
    assert _put(test_client, admin, "multi", repo_roots=["relative/dir"]).status_code == 400
    assert _put(test_client, admin, "multi", repo_roots=[]).status_code == 200 and workspace_service.get_repo_roots("multi") == []


def test_the_stored_shape_stays_readable_by_older_builds_and_old_values_are_read(test_client, admin_token):
    import json as _json

    from app.database import get_db
    from app.services import session_context_service, workspace_service

    admin = {"Authorization": f"Bearer {admin_token}"}
    assert test_client.post("/api/workspaces", headers=admin, json={"id": "legacy", "name": "legacy"}).status_code == 201
    with get_db() as conn:  # a value written by the one-folder build: a plain string
        conn.execute("INSERT OR REPLACE INTO system_settings (key, value) VALUES ('workspace_repo_roots', ?)",
                     (_json.dumps({"workspace:legacy": "/srv/legacy"}),))
        conn.commit()
    assert workspace_service.get_repo_roots("legacy") == ["/srv/legacy"] and workspace_service.get_repo_root("legacy") == "/srv/legacy"
    assert session_context_service.resolve_scope(["workspace:legacy"], None, "/srv/legacy/app")[1] == "cwd"

    def stored():
        with get_db() as conn:
            return _json.loads(conn.execute("SELECT value FROM system_settings WHERE key = 'workspace_repo_roots'").fetchone()[0])["workspace:legacy"]

    # One folder is written back as a string (an older build reads it); several as a list.
    assert _put(test_client, admin, "legacy", repo_roots=["/srv/legacy"]).status_code == 200 and stored() == "/srv/legacy"
    assert _put(test_client, admin, "legacy", repo_roots=["/srv/legacy", "C:\\Users\\me\\legacy"]).status_code == 200
    assert stored() == ["/srv/legacy", "C:\\Users\\me\\legacy"]
    # The old single field: sending the first folder back unchanged keeps the rest (older dashboards do this on every save),
    # a different folder replaces them all, an empty one unlinks.
    assert _put(test_client, admin, "legacy", repo_root="/srv/legacy/").status_code == 200 and len(stored()) == 2
    assert _put(test_client, admin, "legacy", repo_root="/srv/elsewhere").status_code == 200 and stored() == "/srv/elsewhere"
    assert _put(test_client, admin, "legacy", repo_root="").status_code == 200 and workspace_service.get_repo_roots("legacy") == []


def test_the_verifier_uses_the_folder_that_exists_on_the_host_and_skips_the_rest(test_client, tmp_path):
    import json as _json

    from app.database import get_db
    from app.services import verification_service

    (tmp_path / "src").mkdir()
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO system_settings (key, value) VALUES ('workspace_repo_roots', ?)",
                     (_json.dumps({"workspace:v": ["C:\\Users\\me\\proj", str(tmp_path)], "workspace:w": ["C:\\only\\windows"]}),))
        conn.commit()
    status, _why = verification_service._verify_repo("src", {"scope": "workspace:v"})
    assert status == verification_service.VERIFIED
    status, _why = verification_service._verify_repo("missing.py", {"scope": "workspace:v"})
    assert status == verification_service.MISSING
    status, why = verification_service._verify_repo("src", {"scope": "workspace:w"})
    assert status == verification_service.UNVERIFIABLE and "none of the configured folders" in why
