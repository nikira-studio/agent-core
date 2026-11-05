"""The Approvals dashboard page: what an agent proposed, and what a person decides.

The proposal itself comes from an agent; the decision comes from a person in
the dashboard. These tests look at the page a person opens.
"""

from tests.conftest import csrf_headers
from tests.integration.test_approvals import (
    _browser_admin,
    _propose,
    _setup,
    _update_target,
)


def _proposal(test_client, admin_token, operations=None, title="Widen target-agent"):
    """One proposal from an agent with the admin_propose capability."""
    key = _setup(test_client, admin_token)
    r = _propose(test_client, key, operations or [_update_target("approved change")], title=title)
    assert r.status_code == 201, r.text
    return r.json()["data"]["approval"]["id"]


def _regular_user(test_client):
    """Sign the client in as a non-admin who may read the ops workspace."""
    from app.services import workspace_service
    from app.services.auth_service import create_session, create_user

    create_user("regular", "regular@test.local", "testpassword123", "Regular", "user")
    workspace_service.upsert_workspace_collaborator(
        workspace_id="ops", user_id="regular", can_read=True, can_write=False,
        created_by="admin",
    )
    test_client.cookies.clear()
    test_client.cookies.set(
        "session_token", create_session("regular", channel="dashboard")["session_id"]
    )


def test_the_list_shows_a_pending_proposal(test_client, admin_token):
    approval_id = _proposal(test_client, admin_token)
    _browser_admin(test_client)

    r = test_client.get("/approvals")
    assert r.status_code == 200, r.status_code
    html = r.text
    assert "Widen target-agent" in html
    assert f"/approvals/{approval_id}" in html
    assert "ops-agent" in html
    assert "workspace:ops" in html
    assert "pending" in html


def test_the_list_shows_the_approval_again_once_it_is_decided(test_client, admin_token):
    approval_id = _proposal(test_client, admin_token)
    _browser_admin(test_client)
    r = test_client.post(
        f"/api/approvals/{approval_id}/decide",
        json={"approve": False, "note": "use a read-only key"},
        headers=csrf_headers(test_client),
    )
    assert r.status_code == 200, r.text

    html = test_client.get("/approvals").text
    assert "Widen target-agent" in html
    assert "rejected" in html


def test_the_detail_page_shows_the_operation_summary(test_client, admin_token):
    approval_id = _proposal(test_client, admin_token)
    _browser_admin(test_client)

    r = test_client.get(f"/approvals/{approval_id}")
    assert r.status_code == 200, r.status_code
    html = r.text
    # The registry's summary says what the operation does, in the registry's
    # own words, not just the operation's name.
    assert "scopes, capabilities or description" in html
    assert "agent.update" in html
    assert "approved change" in html


def test_the_detail_page_hides_a_param_that_looks_like_a_secret(test_client, admin_token):
    approval_id = _proposal(test_client, admin_token, operations=[{
        "name": "agent.update",
        "params": {
            "agent_id": "target-agent",
            "display_name": "target-agent",
            "description": "approved change",
            "read_scopes": ["workspace:ops"],
            "write_scopes": ["workspace:ops"],
            "capabilities": ["memory"],
            "api_key": "hide-me-1",
        },
    }])
    _browser_admin(test_client)

    html = test_client.get(f"/approvals/{approval_id}").text
    # The parameter is still listed, so the person knows the operation carries
    # one; its value is not for a page to show.
    assert "api_key" in html
    assert "[hidden]" in html
    assert "hide-me-1" not in html


def test_the_admin_is_asked_for_the_values_only_a_person_may_type(test_client, admin_token):
    key = _setup(test_client, admin_token)
    approval_id = _propose(test_client, key, [{"name": "credential.create", "params": {
        "scope": "workspace:ops", "name": "API_TOKEN"}}]).json()["data"]["approval"]["id"]
    _browser_admin(test_client)

    html = test_client.get(f"/approvals/{approval_id}").text
    assert "value for operation 1" in html
    # The label counts operations from 1; the decide endpoint counts them from 0.
    assert "data-person-field='0.value'" in html


def test_an_admin_sees_the_buttons_and_a_regular_user_does_not(test_client, admin_token):
    approval_id = _proposal(test_client, admin_token)
    _browser_admin(test_client)

    admin_html = test_client.get(f"/approvals/{approval_id}").text
    assert "data-approval-approve=" in admin_html
    assert "data-approval-reject=" in admin_html

    _regular_user(test_client)
    user_html = test_client.get(f"/approvals/{approval_id}").text
    assert "data-approval-approve=" not in user_html
    assert "data-approval-reject=" not in user_html
    # The proposal is readable in a scope this person may read; only the
    # decision is not theirs to make.
    assert "Widen target-agent" in user_html


def test_a_decided_approval_has_no_buttons(test_client, admin_token):
    approval_id = _proposal(test_client, admin_token)
    _browser_admin(test_client)
    r = test_client.post(
        f"/api/approvals/{approval_id}/decide",
        json={"approve": False, "note": "use a read-only key"},
        headers=csrf_headers(test_client),
    )
    assert r.status_code == 200, r.text

    html = test_client.get(f"/approvals/{approval_id}").text
    assert "data-approval-approve=" not in html
    assert "data-approval-reject=" not in html
    assert "use a read-only key" in html
    assert "rejected" in html


def test_an_unknown_id_is_a_404(test_client, admin_token):
    _browser_admin(test_client)

    r = test_client.get("/approvals/no-such-approval")
    assert r.status_code == 404, r.status_code
    assert "Approval Not Found" in r.text


def test_a_proposal_in_a_scope_the_person_cannot_read_is_not_shown(test_client, admin_token):
    approval_id = _proposal(test_client, admin_token)
    from app.services.auth_service import create_session, create_user

    create_user("stranger", "stranger@test.local", "testpassword123", "Stranger", "user")
    test_client.cookies.clear()
    test_client.cookies.set(
        "session_token", create_session("stranger", channel="dashboard")["session_id"]
    )

    r = test_client.get(f"/approvals/{approval_id}")
    assert r.status_code == 404, r.status_code
    assert "Widen target-agent" not in r.text
