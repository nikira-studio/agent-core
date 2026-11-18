"""The dialogs around a credential that belongs to a connection: it is made in one step with the connection, the
delete dialog says what happens to it, and the credential is shown with the name of its connection."""

import json
import pathlib
import re
import shutil
import subprocess

import pytest

from app.services import connector_service, credential_service, workspace_service
from app.services.auth_service import create_session, create_user
from tests.conftest import csrf_headers

REPO = pathlib.Path(__file__).resolve().parents[2]
CONNECTORS_JS = REPO / "app" / "dashboard" / "static" / "js" / "connectors.js"
CREDENTIALS_JS = REPO / "app" / "dashboard" / "static" / "js" / "credentials.js"


@pytest.fixture
def world(test_client, clean_db):
    create_user("admin", "admin@test.local", "testpassword123", "Admin", "admin")
    test_client.cookies.set("session_token", create_session("admin", channel="dashboard")["session_id"])
    workspace_service.create_workspace("acc", "Accounts", "admin")
    connector_service.create_connector_type(
        connector_type_id="own-cap", display_name="Owned capability", auth_type="none", provider_type="builtin",
        supported_actions=["ping"], backend_type="http",
        backend_json=json.dumps({"type": "http", "base_url": {"from": "config", "field": "base_url"}, "requests": {}}),
    )
    return {"client": test_client, "headers": csrf_headers(test_client)}


def _make_connection(world, name="Main", credential_name="its-key", owned_by="connection"):
    body = {
        "connector_type_id": "own-cap", "name": name, "scope": "workspace:acc",
        "config_json": '{"base_url": "http://example.test"}',
        "new_credential": {"name": credential_name, "value": "the-secret-value", "owned_by": owned_by},
    }
    made = world["client"].post("/api/connector-bindings", json=body, headers=world["headers"])
    assert made.status_code == 201, made.text
    return made.json()["data"]["binding"]


# ── the Add connection dialog ───────────────────────────────────────────────


def test_the_add_connection_dialog_offers_the_credential_to_the_owner(world):
    html = world["client"].get("/connectors").text
    assert "Also let agents reference this credential" in html
    assert "Off: only Agent Core uses it and no agent can reach it." in html
    assert 'id="binding-new-credential-shared"' in html
    # the checkbox sits under the fields for the new credential, not somewhere else on the page
    create = html.split('id="create-binding-modal"', 1)[1].split('id="edit-binding-modal"', 1)[0]
    assert 'id="binding-new-credential-shared"' in create


def test_the_new_credential_travels_with_the_connection_in_one_step():
    source = CONNECTORS_JS.read_text()
    create = source.split("async function createBinding", 1)[1].split("function toggleBindingCredentialMode", 1)[0]
    assert "new_credential" in create
    assert "owned_by" in create
    # the credential is not made on its own first: the connection, the credential and the link are made together
    assert "/api/credentials/entries" not in create
    body_block = create.split("const body = {", 1)[1].split("};", 1)[0]
    assert "credential_id" not in body_block
    assert "body.new_credential = newCredential" in create
    assert "body.credential_id = credentialId" in create


def test_the_owner_of_the_new_credential_follows_the_owner_select():
    source = CONNECTORS_JS.read_text()
    assert "binding-scope" in source
    assert "updateNewCredentialOwnerHint" in source
    assert "so agents there can reference it." in source


# ── the delete dialog ────────────────────────────────────────────────────────


def test_the_delete_dialog_says_what_happens_to_the_credential():
    source = CONNECTORS_JS.read_text()
    delete = source.split("async function deleteBinding", 1)[1].split("async function deleteConnectorType", 1)[0]
    assert "delete_credential" in delete
    assert "Also delete its credential" in source
    assert "Nothing else can use it once the connection is gone." in source
    assert "still receive it. They keep it unless you delete it too." in source
    # the three answers the person can get
    for message in ("Connection deleted", "Connection and its credential deleted", "Its credential was kept:"):
        assert message in source


def test_the_delete_dialog_is_on_the_page_with_its_two_buttons(world):
    html = world["client"].get("/connectors").text
    assert 'id="delete-binding-modal"' in html
    dialog = html.split('id="delete-binding-modal"', 1)[1].split('id="import-spec-modal"', 1)[0]
    assert "Delete connection" in dialog and "Cancel" in dialog
    assert 'id="delete-binding-credential"' in dialog


def test_the_delete_dialog_reads_the_credential_the_connection_owns(world):
    binding = _make_connection(world)
    info = world["client"].get(f"/api/access/connections/{binding['id']}").json()["data"]
    assert info["owned_credential"]["name"] == "its-key"
    assert info["owned_credential"]["services_holding"] == []


# ── showing a credential that belongs to a connection ───────────────────────


def test_the_credentials_page_names_the_connection_the_credential_belongs_to(world):
    _make_connection(world, name="Main")
    html = world["client"].get("/credentials").text
    assert "Connection Main" in html
    assert "capability:" not in html  # the raw scope is never shown


def test_a_credential_whose_connection_is_gone_says_so(world):
    credential_service.create_credential("capability:no-such-connection", "orphan-key", "not-shown")
    html = world["client"].get("/credentials").text
    assert "A deleted connection" in html
    assert "capability:" not in html


def test_the_who_can_use_this_dialog_says_that_no_agent_can_reach_it():
    source = CREDENTIALS_JS.read_text()
    assert "No agent can reach this. It belongs to" in source
    assert "so only Agent Core uses it." in source
    assert "accessScopeName" in source


def test_the_three_groups_and_their_sentence_stay(world):
    html = world["client"].get("/credentials").text
    dialog = html.split('id="access-modal"', 1)[1].split('id="agent-access-modal"', 1)[0]
    assert "Connections that sign in with this" in dialog
    assert "Agents that can reach this" in dialog
    assert "Services that receive this value" in dialog
    assert "A service receives the secret value itself. An agent using a capability does not." in dialog
    assert "Change owner" in dialog


def test_the_scripts_are_valid_javascript():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    for path in (CONNECTORS_JS, CREDENTIALS_JS):
        result = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
        assert result.returncode == 0, f"{path.name}: {result.stderr}"


def test_the_pages_still_serve_scripts_that_parse_with_a_connection_on_them(world, tmp_path):
    """The connection names travel to the browser as script: a page with a connection on it must still parse."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    _make_connection(world, name="Main")
    problems = []
    for path in ("/connectors", "/credentials"):
        html = world["client"].get(path).text
        for index, script in enumerate(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)):
            if not script.strip():
                continue
            file = tmp_path / f"{path.strip('/')}-{index}.js"
            file.write_text(script)
            result = subprocess.run([node, "--check", str(file)], capture_output=True, text=True)
            if result.returncode:
                problems.append((path, index, result.stderr.strip().splitlines()[:3]))
    assert not problems, problems
