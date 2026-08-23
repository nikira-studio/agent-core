"""Listing a user's workspaces must not list everyone's.

The listing query joins collaborator grants to decide what a user may see. A
LEFT JOIN narrows nothing on its own, so without a predicate beside it the
"workspaces for this user" call returned the whole table — names, descriptions,
and owners of every project on the installation.
"""

from app.services import agent_service, workspace_service
from app.services.auth_service import create_session, create_user


def _user(uid, token_only=False):
    create_user(uid, f"{uid}@test.local", "testpassword123", uid.title(), "user")
    return create_session(uid)["session_id"]


def _make_workspace(client, token, wid, description="private"):
    r = client.post(
        "/api/workspaces",
        headers={"Authorization": f"Bearer {token}"},
        json={"id": wid, "name": wid.title(), "description": description},
    )
    assert r.status_code == 201, r.json()


def _listed(client, token):
    r = client.get("/api/workspaces", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.json()
    return {w["id"] for w in r.json()["data"]["workspaces"]}


def test_a_stranger_sees_nothing(test_client, admin_token):
    owner = _user("owner")
    stranger = _user("stranger")
    _make_workspace(test_client, owner, "privateproj", "unreleased roadmap")

    assert _listed(test_client, stranger) == set(), "no grant, no visibility"


def test_an_owner_sees_their_own(test_client, admin_token):
    owner = _user("owner")
    _make_workspace(test_client, owner, "ownedproj")
    assert "ownedproj" in _listed(test_client, owner)


def test_a_collaborator_sees_what_they_were_granted_and_no_more(test_client, admin_token):
    owner = _user("owner")
    collab = _user("collab")
    _make_workspace(test_client, owner, "sharedproj")
    _make_workspace(test_client, owner, "otherproj")

    r = test_client.put(
        "/api/workspaces/sharedproj/collaborators/collab",
        headers={"Authorization": f"Bearer {owner}"},
        json={"can_read": True, "can_write": False},
    )
    assert r.status_code == 200, r.json()

    assert _listed(test_client, collab) == {"sharedproj"}


def test_a_write_only_grant_does_not_leak_the_listing(test_client, admin_token):
    """The join now requires can_read, matching can_user_read_workspace."""
    owner = _user("owner")
    collab = _user("collab")
    _make_workspace(test_client, owner, "sharedproj")

    test_client.put(
        "/api/workspaces/sharedproj/collaborators/collab",
        headers={"Authorization": f"Bearer {owner}"},
        json={"can_read": False, "can_write": False},
    )
    assert _listed(test_client, collab) == set()


def test_an_admin_still_sees_everything(test_client, admin_token):
    owner = _user("owner")
    _make_workspace(test_client, owner, "privateproj")
    assert "privateproj" in _listed(test_client, admin_token)


def test_the_service_agrees_with_the_per_workspace_check(test_client, admin_token):
    """Two ways of asking the same question must not disagree.

    `can_user_read_workspace` gates a single workspace; the listing gates the
    set. A listing that includes something the per-record check would deny is
    how a leak hides in plain sight.
    """
    owner = _user("owner")
    _user("stranger")
    _make_workspace(test_client, owner, "alpha")
    _make_workspace(test_client, owner, "beta")

    for subject in ("owner", "stranger"):
        listed = {w["id"] for w in workspace_service.list_accessible_workspaces(subject)}
        for workspace_id in ("alpha", "beta"):
            allowed = workspace_service.can_user_read_workspace(subject, workspace_id)
            assert (workspace_id in listed) == allowed, (
                f"{subject}: listing says {workspace_id in listed}, check says {allowed}"
            )


def test_agent_lists_and_reads_only_workspaces_in_its_read_scopes(
    test_client, admin_token
):
    workspace_service.create_workspace("foo", "Foo", "admin")
    workspace_service.create_workspace("bar", "Bar", "admin")
    _, api_key = agent_service.create_agent(
        "catalogagent",
        "Catalog Agent",
        "admin",
        read_scopes=["workspace:foo"],
        write_scopes=[],
    )
    headers = {"Authorization": f"Bearer {api_key}"}

    listed = test_client.get("/api/workspaces", headers=headers)
    assert listed.status_code == 200, listed.json()
    assert [workspace["id"] for workspace in listed.json()["data"]["workspaces"]] == [
        "foo"
    ]

    allowed = test_client.get("/api/workspaces/foo", headers=headers)
    assert allowed.status_code == 200, allowed.json()
    assert allowed.json()["data"]["workspace"]["id"] == "foo"

    denied = test_client.get("/api/workspaces/bar", headers=headers)
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "FORBIDDEN"


def test_agent_without_workspace_scopes_gets_an_empty_catalog(
    test_client, admin_token
):
    workspace_service.create_workspace("private", "Private", "admin")
    _, api_key = agent_service.create_agent(
        "scopelessagent",
        "Scopeless Agent",
        "admin",
        read_scopes=[],
        write_scopes=[],
    )

    response = test_client.get(
        "/api/workspaces", headers={"Authorization": f"Bearer {api_key}"}
    )
    assert response.status_code == 200, response.json()
    assert response.json()["data"]["workspaces"] == []


def test_agent_workspace_key_does_not_gain_management_access(
    test_client, admin_token
):
    workspace_service.create_workspace("readonly", "Read Only", "admin")
    _, api_key = agent_service.create_agent(
        "readonlyagent",
        "Read Only Agent",
        "admin",
        read_scopes=["workspace:readonly"],
        write_scopes=["workspace:readonly"],
    )
    headers = {"Authorization": f"Bearer {api_key}"}

    response = test_client.put(
        "/api/workspaces/readonly",
        headers=headers,
        json={"name": "Agents Cannot Rename Workspaces"},
    )
    assert response.status_code == 401
