"""Who can use a credential: the dialog's data, its page affordances, and who may see them."""

import pytest

from tests.conftest import csrf_headers
from app.services import agent_service, connector_service, credential_service, workspace_service
from app.services.auth_service import create_session, create_user

SCOPE = "workspace:acc"


@pytest.fixture
def admin(test_client, clean_db):
    create_user("admin", "admin@test.local", "testpassword123", "Admin", "admin")
    test_client.cookies.set("session_token", create_session("admin", channel="dashboard")["session_id"])
    workspace_service.create_workspace("acc", "Acc", "admin")
    for agent_id, caps in (
        ("svc-a", ["credentials_resolve"]),
        ("svc-b", ["credentials_resolve"]),
        ("asst", ["memory", "coordination", "credentials"]),
    ):
        agent_service.create_agent(
            agent_id=agent_id, display_name=agent_id.upper(), owner_user_id="admin",
            read_scopes=[SCOPE], write_scopes=[], capabilities=caps,
        )
    return test_client


def _entry():
    return credential_service.create_credential(SCOPE, "svc-key", "not-shown")


def _access(client, entry_id):
    r = client.get(f"/api/credential-consumers/entries/{entry_id}/access")
    assert r.status_code == 200, r.text
    return r.json()["data"]


def test_a_new_credential_has_no_holders_and_says_who_could_receive_it(admin):
    entry = _entry()
    data = _access(admin, entry["id"])
    assert data["grants"] == [] and data["services"] == []
    assert {a["id"] for a in data["candidates"]} == {"svc-a", "svc-b"}
    assert [a["id"] for a in data["cannot_receive"]] == ["asst"]  # needs the role first
    assert data["scope_readers"] == ["ASST"]  # reads the scope, so it already sees the reference
    assert "not-shown" not in str(data)


def test_giving_and_removing_access_changes_the_dialog_and_is_audited(admin):
    entry = _entry()
    given = admin.post("/api/credential-consumers/grants", json={"consumer_agent_id": "svc-a", "entry_id": entry["id"], "allow_write": False}, headers=csrf_headers(admin))
    assert given.status_code == 201, given.text
    data = _access(admin, entry["id"])
    assert [g["agent_id"] for g in data["grants"]] == ["svc-a"]
    assert {a["id"] for a in data["candidates"]} == {"svc-b"}

    revoked = admin.delete(f"/api/credential-consumers/grants/{data['grants'][0]['id']}", headers=csrf_headers(admin))
    assert revoked.status_code == 200
    again = _access(admin, entry["id"])
    assert again["grants"] == [] and {a["id"] for a in again["candidates"]} == {"svc-a", "svc-b"}

    from app.database import get_db

    with get_db() as conn:
        actions = [r["action"] for r in conn.execute("SELECT action FROM audit_log WHERE action LIKE 'credential_consumer_grant_%'")]
    assert "credential_consumer_grant_created" in actions and "credential_consumer_grant_revoked" in actions


def test_an_agent_without_the_permission_cannot_be_given_the_credential(admin):
    entry = _entry()
    r = admin.post("/api/credential-consumers/grants", json={"consumer_agent_id": "asst", "entry_id": entry["id"], "allow_write": False}, headers=csrf_headers(admin))
    assert r.status_code == 403 and r.json()["error"]["code"] == "CAPABILITY_DENIED"


def test_the_dialog_lists_the_services_that_use_the_credential(admin):
    entry = _entry()
    connector_service.create_connector_type(
        connector_type_id="acc-svc", display_name="Acc service", auth_type="none",
        supported_actions=[{"name": "GET /x", "side_effect": "read"}], provider_type="openapi", backend_type="generic_http",
    )
    connector_service.create_binding("acc-svc", "Acc binding", SCOPE, credential_id=entry["id"])
    services = _access(admin, entry["id"])["services"]
    assert [(s["name"], s["service"]) for s in services] == [("Acc binding", "Acc service")]


def test_the_credentials_page_shows_who_uses_each_one_to_an_admin(admin):
    entry = _entry()
    admin.post("/api/credential-consumers/grants", json={"consumer_agent_id": "svc-a", "entry_id": entry["id"], "allow_write": False}, headers=csrf_headers(admin))
    html = admin.get("/credentials").text
    assert "<th>Used by</th>" in html and f"data-credential-access='{entry['id']}'" in html
    assert "SVC-A" in html
    dialog = html.split('id="access-modal"', 1)[1]
    assert "One credential per service" in dialog and "Use a separate credential only for a different account" in dialog
    assert "<summary>Advanced</summary>" in dialog  # expiry and write stay out of the first view
    assert 'id="access-expires"' in dialog.split("<summary>Advanced</summary>", 1)[1]


def test_a_regular_user_gets_no_access_controls_and_no_access_data(admin):
    entry = _entry()
    create_user("regular", "regular@test.local", "testpassword123", "Regular", "user")
    admin.cookies.clear()
    admin.cookies.set("session_token", create_session("regular", channel="dashboard")["session_id"])
    assert "data-credential-access" not in admin.get("/credentials").text
    assert "<th>Used by</th>" not in admin.get("/credentials").text
    assert admin.get(f"/api/credential-consumers/entries/{entry['id']}/access").status_code == 403


def test_the_agents_page_links_service_agents_to_their_credentials_and_roles_deep_link(admin):
    html = admin.get("/agents").text
    assert "href='/credentials?agent=svc-a'" in html and "href='/credentials?agent=svc-b'" in html
    assert "href='/credentials?agent=asst'" not in html  # an assistant does not take grants
    assert "get('edit')" in html  # /agents?edit=<id> opens that agent's role
