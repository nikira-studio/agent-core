"""Release I: the words a person reads.

- a connection has an owner, and the dialog says so;
- the list pages say the owner in words, never as a scope string;
- every "Add" picker asks which one before anything is added;
- the services have one home: the Credentials section.
"""

import json
import pathlib
import re

import pytest

from app.services import agent_service, connector_service, credential_service, workspace_service
from app.services.auth_service import create_session, create_user
from tests.conftest import csrf_headers

REPO = pathlib.Path(__file__).resolve().parents[2]
CREDENTIALS_JS = REPO / "app" / "dashboard" / "static" / "js" / "credentials.js"
AGENT_ACCESS_JS = REPO / "app" / "dashboard" / "static" / "js" / "agent_access.js"


@pytest.fixture
def world(test_client, clean_db):
    """An admin at the dashboard, another person, a workspace, an agent, a service and a credential."""
    create_user("admin", "admin@test.local", "testpassword123", "Admin", "admin")
    create_user("alex", "alex@test.local", "testpassword123", "Alex", "user")
    test_client.cookies.set("session_token", create_session("admin", channel="dashboard")["session_id"])
    workspace_service.create_workspace("acc", "Accounts", "alex")
    agent_service.create_agent(
        "worker", "Worker", "admin", default_user_id="alex",
        capabilities=["memory", "credentials"], read_scopes=["workspace:acc"], write_scopes=[],
    )
    agent_service.create_agent("reader", "Reader", "admin", kind="service")
    entry = credential_service.create_credential("workspace:acc", "shared-key", "not-shown")
    connector_service.create_connector_type(
        connector_type_id="ri-cap", display_name="Release I capability", auth_type="none", provider_type="builtin",
        supported_actions=["ping"], backend_type="http",
        backend_json=json.dumps({"type": "http", "requests": {"ping": {"method": "GET", "path": "/ping"}}}),
    )
    return {"client": test_client, "entry": entry, "headers": csrf_headers(test_client)}


def _picker(html, element_id):
    """The options of one select, as the page carries them."""
    return html.split(f'id="{element_id}"', 1)[1].split("</select>", 1)[0]


def _row(html, marker):
    """The table row that carries the marker."""
    for chunk in html.split("<tr"):
        if marker in chunk:
            return chunk.split("</tr>", 1)[0]
    return ""


def _words(html):
    """The page's words, with the entities a browser would read as characters."""
    for entity, character in (("&gt;", ">"), ("&lt;", "<"), ("&quot;", '"'), ("&#39;", "'"), ("&amp;", "&")):
        html = html.replace(entity, character)
    return html


def _block_with(source, needle):
    """The function in a script that mentions the given id."""
    index = source.index(needle)
    bounds = [m.start() for m in re.finditer(r"\n(?:async )?function ", source)]
    before = [b for b in bounds if b < index]
    after = [b for b in bounds if b > index]
    start = before[-1] if before else 0
    end = after[0] if after else len(source)
    return source[start:end]


# ── 1. the connection dialogs ───────────────────────────────────────────────


def test_the_owner_picker_offers_the_person_the_workspaces_and_the_active_agents(world):
    agent_service.create_agent("sleepy", "Sleepy", "admin", capabilities=["memory"])
    agent_service.deactivate_agent("sleepy")
    html = world["client"].get("/connectors").text
    picker = _picker(html, "binding-scope")
    assert 'value="user:admin"' in picker
    assert 'value="workspace:acc"' in picker
    assert 'value="agent:worker"' in picker
    # A service only fetches secrets, so it is never an owner; neither is an agent that is switched off.
    assert 'value="agent:reader"' not in picker
    assert 'value="agent:sleepy"' not in picker
    assert 'optgroup label="You"' in picker
    assert 'optgroup label="Workspaces"' in picker
    assert 'optgroup label="Agents (private to one agent)"' in picker
    # The edit dialog offers the same owners.
    edit_picker = _picker(html, "edit-binding-scope")
    for owner in ('value="user:admin"', 'value="workspace:acc"', 'value="agent:worker"'):
        assert owner in edit_picker, owner


def test_the_connection_dialogs_call_the_owner_the_owner(world):
    html = world["client"].get("/connectors").text
    create = html.split('id="create-binding-modal"', 1)[1].split('id="edit-binding-modal"', 1)[0]
    edit = html.split('id="edit-binding-modal"', 1)[1].split('id="delete-binding-modal"', 1)[0]
    for dialog in (create, edit):
        assert "Who may use it" not in dialog
        assert "The owner can edit this connection. Choose who can use it afterwards, under Who can use it." in dialog
    assert "<label>Owner *</label>" in create
    assert "<label>Owner</label>" in edit
    assert '<option value="">Choose an owner...</option>' in create
    assert "Select a workspace..." not in html
    assert (
        "A connection signs in to a capability with one credential. Its owner can change it. "
        "Who else can use it is set afterwards, under Who can use it."
    ) in create
    assert (
        "A connection links a capability to a credential. Its owner can edit it; "
        "who can use it is set under Who can use it."
    ) in html


def test_the_connections_table_names_the_owner_in_words(world):
    connector_service.create_binding(
        "ri-cap", "Main connection", "workspace:acc", config_json='{"base_url": "http://example.test"}'
    )
    connector_service.create_binding(
        "ri-cap", "Alex's connection", "user:alex", config_json='{"base_url": "http://example.test"}'
    )
    html = world["client"].get("/connectors").text
    table = html.split('id="bindings-list"', 1)[1].split("</table>", 1)[0]
    assert "<th>Owner</th>" in table
    assert 'data-label="Owner"' in table
    assert ">Accounts</td>" in table
    # Another person is named, not shown as an id.
    assert "Alex's personal space" in _words(table)
    assert "user:alex" not in table
    assert "workspace:acc" not in table


def test_the_stored_credentials_are_offered_with_their_owner_in_words(world):
    connector_service.create_binding(
        "ri-cap", "Main connection", "workspace:acc",
        credential_id=world["entry"]["id"], config_json='{"base_url": "http://example.test"}',
    )
    html = world["client"].get("/connectors").text
    picker = _picker(html, "binding-credential")
    assert "shared-key (Accounts / AC_SECRET_" in picker


# ── 2. the list pages ───────────────────────────────────────────────────────


def test_the_agents_row_says_what_it_can_reach_in_words(world):
    html = world["client"].get("/agents").text
    row = _row(html, "data-agent-edit='worker'")
    assert "private memory" in row
    assert "acts for Alex" in row
    assert "1 workspace" in row
    assert "workspace:acc" not in row
    assert "agent:worker" not in row
    assert "user:alex" not in row
    # The old count of grants is gone.
    assert "read /" not in row and "write grants" not in row


def test_the_agents_row_counts_the_workspaces_it_is_given(world):
    workspace_service.create_workspace("other", "Other Place", "admin")
    agent_service.create_agent(
        "two-places", "Two Places", "admin",
        capabilities=["memory"], read_scopes=["workspace:acc", "workspace:other"], write_scopes=[],
    )
    html = world["client"].get("/agents").text
    row = _row(html, "data-agent-edit='two-places'")
    assert "2 workspaces" in row
    # The names are on hover.
    assert "Accounts, Other Place" in row


def test_a_service_row_says_how_many_secrets_it_may_fetch(world):
    html = world["client"].get("/services").text
    row = _row(html, "data-agent-edit='reader'")
    assert "no secrets yet" in row
    # A service has no scopes of its own to show.
    assert "private memory" not in row
    assert "acts for" not in row

    granted = world["client"].post(
        "/api/credential-consumers/grants",
        json={"consumer_agent_id": "reader", "entry_id": world["entry"]["id"], "allow_write": False},
        headers=world["headers"],
    )
    assert granted.status_code == 201, granted.text
    html = world["client"].get("/services").text
    row = _row(html, "data-agent-edit='reader'")
    assert "1 secret" in row
    assert "shared-key" in row  # the name is on hover


def test_the_credentials_table_calls_the_owner_the_owner(world):
    html = world["client"].get("/credentials").text
    assert "<th class='col-scope'>Owner</th>" in html
    assert "<span class='scope-name'>Accounts</span>" in html
    assert "<span class='scope-kind' title='workspace Accounts'>workspace</span>" in html


def test_the_credentials_used_by_cell_says_how_many_agents_can_reach_it(world):
    html = world["client"].get("/credentials").text
    row = html.split('data-credential-id="', 1)[1].split("</tr>", 1)[0]
    assert "1 agent can reach it" in row
    assert "can see its name" not in row


def test_the_services_page_says_what_the_page_is_for(world):
    html = world["client"].get("/services").text
    assert (
        "Programs and containers that only fetch secrets. Open a service's Access to give it a secret, "
        "or open a credential's Access."
    ) in html


# ── 3. the pickers ask first ────────────────────────────────────────────────


def test_every_add_picker_starts_on_choose_and_its_add_button_is_disabled():
    for source, pickers in (
        (CREDENTIALS_JS.read_text(), ["access-connection-pick", "access-service-pick", "agent-access-pick"]),
        (AGENT_ACCESS_JS.read_text(), [
            "access-agent-workspace-pick",
            "access-agent-capability-pick",
            "access-service-secret-pick",
            "access-connection-add-ref",
        ]),
    ):
        assert "accessChooseOption" in source
        for picker in pickers:
            block = _block_with(source, f"getElementById('{picker}')")
            assert block, picker
            # The picker starts on "Choose...", and the Add button opens only once something is chosen.
            assert "accessChooseOption" in block, picker
            assert "disabled = true" in block, picker


def test_the_change_of_a_picker_opens_its_add_button():
    for source in (CREDENTIALS_JS.read_text(), AGENT_ACCESS_JS.read_text()):
        assert "add.disabled = !ev.target.value" in source


def test_the_pickers_of_the_dialogs_are_the_ones_that_ask_first(world):
    """The four dialogs that add something each have a picker that starts empty."""
    credentials_html = world["client"].get("/credentials").text
    assert 'id="access-connection-pick"' in credentials_html
    assert 'id="access-service-pick"' in credentials_html
    agents_html = world["client"].get("/agents").text
    assert 'id="access-agent-workspace-pick"' in agents_html
    assert 'id="access-agent-capability-pick"' in agents_html
    services_html = world["client"].get("/services").text
    assert 'id="access-service-secret-pick"' in services_html
    connectors_html = world["client"].get("/connectors").text
    assert 'id="access-connection-add-ref"' in connectors_html
    # The Add buttons start closed, before any script has run.
    for html, button_id in (
        (credentials_html, "access-connection-add"),
        (credentials_html, "access-service-add"),
        (credentials_html, "agent-access-add"),
        (agents_html, "access-agent-workspace-add"),
        (agents_html, "access-agent-capability-add"),
        (services_html, "access-service-secret-add"),
        (connectors_html, "access-connection-add"),
    ):
        button = html.split(f'id="{button_id}"', 1)[1].split(">", 1)[0]
        assert "disabled" in button, button_id


# ── 4. one home for the services ────────────────────────────────────────────


def test_there_is_no_switch_between_the_agents_and_the_services(world):
    for path in ("/agents", "/services"):
        html = world["client"].get(path).text
        assert "agent-switch" not in html, path
        assert "Agents (" not in html, path
        assert "Services (" not in html, path


def test_the_agents_page_points_at_the_services(world):
    html = world["client"].get("/agents").text
    assert 'href="/services"' in html
    assert "Programs and containers that only fetch secrets are under" in html
    assert "Credentials &gt; Services" in html


def test_the_old_address_of_the_services_list_still_works(world):
    r = world["client"].get("/agents?show=services", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/services"


def test_the_credential_dialogs_call_the_field_owner(test_client, clean_db):
    from app.services.auth_service import create_session, create_user

    create_user("admin", "admin@test.local", "testpassword123", "Admin", "admin")
    test_client.cookies.set("session_token", create_session("admin", channel="dashboard")["session_id"])
    html = test_client.get("/credentials").text
    assert "Where it lives" not in html
    assert html.count("<label>Owner") >= 2
