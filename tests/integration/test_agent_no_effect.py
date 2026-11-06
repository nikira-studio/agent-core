"""An agent given a workspace its person cannot reach: say so instead of looking done."""

from app.services import agent_access_service, agent_service, auth_service, workspace_service
from tests.conftest import csrf_headers
from tests.integration.test_approvals import _browser_admin


def _setup():
    auth_service.create_user("pat", "pat@test.local", "testpassword123", "Pat Person", "user")
    workspace_service.create_workspace("alpha", "Alpha Project", "admin")
    workspace_service.create_workspace("beta", "Beta Project", "admin")
    workspace_service.upsert_workspace_collaborator(workspace_id="alpha", user_id="pat", can_read=True, can_write=False, created_by="admin")
    agent_service.create_agent(
        agent_id="pat-agent", display_name="Pat Agent", owner_user_id="admin", default_user_id="pat",
        read_scopes=["workspace:alpha", "workspace:beta"], write_scopes=["workspace:alpha", "workspace:beta"],
        capabilities=["memory"],
    )
    return agent_service.get_agent_by_id("pat-agent")


def test_it_names_the_workspace_the_person_and_what_to_do(test_client, admin_token):
    agent = _setup()
    found = {n["workspace"]: n for n in agent_access_service.no_effect(agent)}
    assert found["beta"]["reason"] == "not_member"
    assert found["beta"]["message"] == "No effect yet: Pat Person is not a member of Beta Project. Add them on Workspaces."
    assert found["alpha"]["reason"] == "read_only_member"
    assert "can read Alpha Project but not write there" in found["alpha"]["message"]


def test_a_member_with_write_has_nothing_to_warn_about(test_client, admin_token):
    agent = _setup()
    workspace_service.upsert_workspace_collaborator(workspace_id="alpha", user_id="pat", can_read=True, can_write=True, created_by="admin")
    workspace_service.upsert_workspace_collaborator(workspace_id="beta", user_id="pat", can_read=True, can_write=True, created_by="admin")
    assert agent_access_service.no_effect(agent) == []


def test_an_inactive_workspace_and_a_disabled_person_are_named_too(test_client, admin_token):
    agent = _setup()
    workspace_service.deactivate_workspace("beta")
    reasons = {n["workspace"]: n["reason"] for n in agent_access_service.no_effect(agent)}
    assert reasons["beta"] == "workspace_inactive"


def test_the_agents_page_edit_form_and_connect_say_it(test_client, admin_token):
    _setup()
    _browser_admin(test_client)
    page = test_client.get("/agents").text
    assert "No effect yet: Pat Person is not a member of Beta Project" in page
    detail = test_client.get("/api/agents/pat-agent", headers=csrf_headers(test_client)).json()["data"]["agent"]
    assert [n["workspace"] for n in detail["no_effect"]] == ["alpha", "beta"]
    assert 'id="edit-no-effect"' in page
    connect = test_client.get("/integrations?user_id=admin&agent_id=pat-agent&workspace_id=beta").text
    assert "No effect yet: Pat Person is not a member of Beta Project" in connect
    assert "Recommended: add workspace scope to agent" not in connect
