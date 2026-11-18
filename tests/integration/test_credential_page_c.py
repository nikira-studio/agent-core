"""The Credential page's "Who can use this" list: the three groups it shows, the pieces it replaced, and the
writes it makes."""

import json

import pytest

from app.services import agent_service, connector_service, credential_service, workspace_service
from app.services.auth_service import create_session, create_user
from tests.conftest import csrf_headers

SCOPE = "workspace:acc"


@pytest.fixture
def admin(test_client, clean_db):
    create_user("admin", "admin@test.local", "testpassword123", "Admin", "admin")
    test_client.cookies.set("session_token", create_session("admin", channel="dashboard")["session_id"])
    workspace_service.create_workspace("acc", "Accounts", "admin")
    workspace_service.create_workspace("other", "Other Place", "admin")
    agent_service.create_agent(
        "worker-a", "Worker A", "admin",
        capabilities=["memory", "credentials", "connectors_read", "connectors_execute"],
        read_scopes=[SCOPE], write_scopes=[],
    )
    agent_service.create_agent(
        "worker-b", "Worker B", "admin",
        capabilities=["memory"], read_scopes=[SCOPE], write_scopes=[],
    )
    agent_service.create_agent("svc-a", "Service A", "admin", kind="service")
    return test_client


def _entry():
    return credential_service.create_credential(SCOPE, "svc-key", "not-shown")


def _dialog(html):
    """The dialog's own markup, without the table around it or the other dialogs after it."""
    return html.split('id="access-modal"', 1)[1].split('id="agent-access-modal"', 1)[0]


def _words(html):
    """The page's words, with the entities a browser would read as characters."""
    for entity, character in (("&gt;", ">"), ("&lt;", "<"), ("&quot;", '"'), ("&#39;", "'"), ("&amp;", "&")):
        html = html.replace(entity, character)
    return html


# ── the page ───────────────────────────────────────────────────────────────


def test_the_page_shows_one_list_of_who_can_use_this_in_three_groups(admin):
    dialog = _dialog(admin.get("/credentials").text)
    assert "Capabilities that sign in with this" in dialog
    assert "Agents that can reach this" in dialog
    assert "Services that receive this value" in dialog
    # the one line that says what the list is for, unchanged
    assert "Store each secret once" in dialog
    assert "Use a separate credential only for a different account" in dialog
    assert "Who can use <span id=\"access-name\"></span>" in dialog


def test_the_services_group_says_who_gets_the_value_and_who_does_not(admin):
    dialog = _dialog(admin.get("/credentials").text)
    assert "A service receives the secret value itself. An agent using a capability does not." in dialog
    assert "Agents get a reference, never the secret value; Agent Core fills it in when a tool runs." in dialog


def test_the_owner_can_be_changed_from_the_list(admin):
    dialog = _dialog(admin.get("/credentials").text)
    assert "Change owner" in dialog
    assert 'id="access-owner-pick"' in dialog
    assert 'id="access-owner-save"' in dialog and 'id="access-owner-cancel"' in dialog


def test_an_empty_service_list_says_where_to_make_one(admin):
    dialog = _words(_dialog(admin.get("/credentials").text))
    assert "No service exists yet. Create one under Credentials > Services." in dialog
    assert 'href="/services"' in dialog


def test_the_dialog_no_longer_shows_the_pieces_it_replaced(admin):
    dialog = _dialog(admin.get("/credentials").text)
    assert ">Given the secret</h3>" not in dialog
    assert "Others who can see its name" not in dialog
    assert "Can't receive it yet" not in dialog
    assert "<summary>Advanced</summary>" not in dialog
    assert 'id="access-grants"' not in dialog
    assert 'id="access-cannot"' not in dialog
    assert 'id="access-scope-readers"' not in dialog


def test_the_used_by_column_is_still_on_the_page(admin):
    _entry()
    html = admin.get("/credentials").text
    assert "<th class='col-used'>Used by</th>" in html


# ── the three groups, as the list reads them ───────────────────────────────


def _connection(entry_id):
    connector_service.create_connector_type(
        connector_type_id="cap-one", display_name="GitHub", auth_type="none", provider_type="builtin",
        supported_actions=["ping"], backend_type="http",
        backend_json=json.dumps({"type": "http", "base_url": {"from": "config", "field": "base_url"}, "requests": {}}),
    )
    return connector_service.create_binding(
        "cap-one", "account A", SCOPE, credential_id=entry_id, config_json='{"base_url": "http://example.test"}'
    )


def test_the_capabilities_group_names_the_connection_and_who_can_run_it(admin):
    entry = _entry()
    _connection(entry["id"])
    data = admin.get(f"/api/access/credentials/{entry['id']}").json()["data"]
    assert [c["name"] for c in data["connections"]] == ["account A"]
    assert data["connections"][0]["capability"] == "GitHub"
    assert data["connections"][0]["enabled"] is True
    users = {u["agent_id"]: u for u in data["connections"][0]["users"]}
    assert set(users) == {"worker-a", "worker-b"}
    assert users["worker-a"]["status"] == "can_use"
    assert users["worker-b"]["status"] == "needs_permission"
    assert users["worker-b"]["needs_label"] == "Run capabilities"


def test_the_agents_group_says_why_each_agent_can_reach_it(admin):
    entry = _entry()
    data = admin.get(f"/api/access/credentials/{entry['id']}").json()["data"]
    agents = {a["agent_id"]: a for a in data["agents"]}
    assert set(agents) == {"worker-a", "worker-b"}
    assert agents["worker-a"]["reason"] == "through workspace Accounts"
    assert agents["worker-a"]["status"] == "can_reference"
    assert agents["worker-b"]["status"] == "needs_permission"
    assert agents["worker-b"]["needs_label"] == "Reference credentials"
    assert data["credential"]["owner"] == "workspace Accounts"


def test_the_services_group_lists_what_receives_the_value_and_what_can_be_added(admin):
    entry = _entry()
    data = admin.get(f"/api/access/credentials/{entry['id']}").json()["data"]
    assert data["services"] == []
    assert data["services_to_add"] == [{"id": "svc-a", "display_name": "Service A"}]
    assert data["held_by_agents"] == []


def test_a_service_can_be_given_the_value_and_taken_away_again(admin):
    entry = _entry()
    headers = csrf_headers(admin)
    given = admin.post(
        "/api/credential-consumers/grants",
        json={"consumer_agent_id": "svc-a", "entry_id": entry["id"], "allow_write": False},
        headers=headers,
    )
    assert given.status_code == 201, given.text
    data = admin.get(f"/api/access/credentials/{entry['id']}").json()["data"]
    assert [s["display_name"] for s in data["services"]] == ["Service A"]
    assert data["services"][0]["allow_write"] is False
    assert data["services_to_add"] == []  # it already has it

    removed = admin.delete(f"/api/credential-consumers/grants/{data['services'][0]['grant_id']}", headers=headers)
    assert removed.status_code == 200, removed.text
    again = admin.get(f"/api/access/credentials/{entry['id']}").json()["data"]
    assert again["services"] == [] and again["services_to_add"] == [{"id": "svc-a", "display_name": "Service A"}]


def test_a_connection_can_be_given_the_credential_and_taken_away_again(admin):
    entry = _entry()
    binding = _connection(entry["id"])
    headers = csrf_headers(admin)
    taken = admin.put(f"/api/connector-bindings/{binding['id']}", json={"reset_credential": True}, headers=headers)
    assert taken.status_code == 200, taken.text
    assert admin.get(f"/api/access/credentials/{entry['id']}").json()["data"]["connections"] == []
    given = admin.put(
        f"/api/connector-bindings/{binding['id']}", json={"credential_id": entry["id"]}, headers=headers
    )
    assert given.status_code == 200, given.text
    assert [c["name"] for c in admin.get(f"/api/access/credentials/{entry['id']}").json()["data"]["connections"]] == ["account A"]


# ── changing the owner ─────────────────────────────────────────────────────


def test_changing_the_owner_is_previewed_before_it_is_saved(admin):
    entry = _entry()
    preview = admin.post(
        "/api/access/preview/credential-owner",
        json={"entry_id": entry["id"], "scope": "workspace:other"},
        headers=csrf_headers(admin),
    ).json()["data"]
    assert [a["display_name"] for a in preview["lose"]] == ["Worker A", "Worker B"]
    assert preview["gain"] == []
    assert preview["unchanged"] == 0


def test_saving_the_owner_moves_the_credential_and_who_can_reach_it(admin):
    entry = _entry()
    moved = admin.put(
        f"/api/credentials/entries/{entry['id']}",
        json={"scope": "workspace:other"},
        headers=csrf_headers(admin),
    )
    assert moved.status_code == 200, moved.text
    data = admin.get(f"/api/access/credentials/{entry['id']}").json()["data"]
    assert data["credential"]["owner"] == "workspace Other Place"
    assert data["agents"] == []  # neither worker reaches it any more
    assert "workspace:other" not in str(data["agents"])
