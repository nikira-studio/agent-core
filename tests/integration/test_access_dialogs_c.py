"""Release C part 2: the Access dialogs of an agent, a service and a connection.

Each of the three says what one thing can reach: an agent's workspaces, capabilities, credentials and keys; a
service's secrets; a connection's users. The names here are made up for the tests.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.services import agent_service, connector_service, credential_service, workspace_service
from app.services.auth_service import create_session, create_user
from tests.conftest import csrf_headers

JS_FILE = Path(__file__).resolve().parents[2] / "app" / "dashboard" / "static" / "js" / "agent_access.js"


@pytest.fixture
def world(test_client, clean_db):
    """An admin at the dashboard, an agent that works, a service that fetches, a credential and a connection."""
    create_user("admin", "admin@test.local", "testpassword123", "Admin", "admin")
    create_user("alex", "alex@test.local", "testpassword123", "Alex", "user")
    test_client.cookies.set("session_token", create_session("admin", channel="dashboard")["session_id"])
    workspace_service.create_workspace("acc", "Accounts", "admin")
    workspace_service.create_workspace("other", "Other place", "admin")
    agent_service.create_agent(
        "worker-a", "Worker A", "admin",
        capabilities=["memory", "credentials"],
        read_scopes=["workspace:acc"], write_scopes=[],
    )
    # Worker B may run the connection; Worker A may not, so its entry needs the permission.
    agent_service.create_agent(
        "worker-b", "Worker B", "admin",
        capabilities=["memory", "connectors_read", "connectors_execute"],
        read_scopes=["workspace:acc"], write_scopes=[],
    )
    agent_service.create_agent("batch-reader", "Batch Reader", "admin", kind="service")
    entry = credential_service.create_credential("workspace:acc", "shared-key", "not-shown")
    connector_service.create_connector_type(
        connector_type_id="acc-cap", display_name="Account capability", auth_type="none", provider_type="builtin",
        supported_actions=["ping"], backend_type="http",
        backend_json=json.dumps({"type": "http", "base_url": {"from": "config", "field": "base_url"}, "requests": {}}),
    )
    binding = connector_service.create_binding(
        "acc-cap", "Account connection", "workspace:acc", credential_id=entry["id"],
        config_json='{"base_url": "http://example.test"}',
    )
    return {"client": test_client, "entry": entry, "binding": binding, "headers": csrf_headers(test_client)}


# ── the pages offer the dialogs ──────────────────────────────────────────────


def test_the_agents_page_offers_the_access_dialog_of_each_agent(world):
    html = world["client"].get("/agents").text
    assert "What " in html and "can do and reach" in html
    assert "data-agent-access='worker-a'" in html
    # A service has its own dialog on the Services page; the Agents page has no button for one.
    assert "data-service-access" not in html


def test_the_services_page_offers_the_access_dialog_and_no_credentials_link(world):
    html = world["client"].get("/services").text
    assert "data-service-access='batch-reader'" in html
    assert "A service receives the secret value itself." in html
    # The row's Credentials link is gone; the dialog says the same thing in one place.
    assert "/credentials?agent=" not in html


def test_the_capabilities_page_offers_who_can_use_it_on_each_connection(world):
    html = world["client"].get("/connectors").text
    assert "Who can use it" in html
    assert f"data-binding-users='{world['binding']['id']}'" in html


def test_the_dialog_sections_are_named_in_the_page(world):
    agents = world["client"].get("/agents").text
    assert "Workspaces" in agents
    assert "Credentials it can reference" in agents
    assert "Capabilities" in agents
    services = world["client"].get("/services").text
    assert "Secrets it can fetch" in services


def test_the_agent_dialog_offers_a_workspace_to_add(world):
    """The row that adds one: a pick of the workspaces that are not listed yet, Read and Write, and Add."""
    html = world["client"].get("/agents").text
    assert "Add a workspace" in html
    assert 'id="access-agent-workspace-pick"' in html
    assert 'id="access-agent-workspace-read"' in html
    assert 'id="access-agent-workspace-write"' in html
    assert 'id="access-agent-workspace-add"' in html


def test_the_service_dialog_offers_a_secret_to_add(world):
    """The row that adds one: a pick of the secrets, the write choice, the end date, and Add."""
    html = world["client"].get("/services").text
    assert "Add a secret" in html
    assert 'id="access-service-secret-pick"' in html
    assert 'id="access-service-secret-write"' in html
    assert "Allow it to write the value back" in html
    assert "Access ends on" in html
    assert 'id="access-service-secret-add"' in html


def test_the_connection_dialog_has_a_place_for_the_users(world):
    html = world["client"].get("/connectors").text
    assert 'Who can use <span id="access-connection-name"></span>' in html
    assert 'id="access-connection-users"' in html


def test_the_old_address_of_the_services_dialog_still_works(world):
    """The link is gone from the list; the page behind it still opens."""
    r = world["client"].get("/credentials?agent=batch-reader")
    assert r.status_code == 200


def test_the_agents_page_carries_the_workspaces_the_dialog_offers(world):
    """The pick of workspaces to add is built from the list the page's own forms already have."""
    html = world["client"].get("/agents").text
    assert "const ACCESS_WORKSPACES = " in html
    assert "/static/js/agent_access.js" in html


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_access_script_parses_and_keeps_the_words_the_dialog_shows():
    """The strings the dialogs are built from live in the script, so they are checked here."""
    result = subprocess.run(["node", "--check", str(JS_FILE)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    script = JS_FILE.read_text()
    for phrase in (
        "Add, rename or revoke keys under More > Keys.",
        "No agent can use it right now.",
        "No credential needed",
        "Signs in with: ",
        "needs permission: ",
        "can use its reference",
        "reads and writes",
        "no effect yet",
    ):
        assert phrase in script, phrase


# ── the answers the dialogs show ────────────────────────────────────────────


def test_the_agents_dialog_shows_the_workspaces_the_capabilities_and_the_credentials(world):
    data = world["client"].get("/api/access/agents/worker-a").json()["data"]
    assert [w["name"] for w in data["workspaces"]] == ["Accounts"]
    assert [c["name"] for c in data["connections"]] == ["Account connection"]
    assert [c["name"] for c in data["credentials"]] == ["shared-key"]
    assert "not-shown" not in json.dumps(data)


def test_the_services_dialog_shows_the_secrets_it_may_fetch_and_the_ones_it_may_not(world):
    client, headers = world["client"], world["headers"]
    granted = client.post(
        "/api/credential-consumers/grants",
        json={"consumer_agent_id": "batch-reader", "entry_id": world["entry"]["id"], "allow_write": True},
        headers=headers,
    )
    assert granted.status_code == 201, granted.text
    data = client.get("/api/access/services/batch-reader").json()["data"]
    assert [g["name"] for g in data["grants"]] == ["shared-key"]
    assert data["grants"][0]["allow_write"] is True
    assert "not-shown" not in json.dumps(data)


def test_the_connections_dialog_says_who_can_use_it(world):
    data = world["client"].get(f"/api/access/connections/{world['binding']['id']}").json()["data"]
    assert data["credential"]["name"] == "shared-key"
    by_agent = {u["agent_id"]: u for u in data["users"]}
    assert set(by_agent) == {"worker-a", "worker-b"}
    assert by_agent["worker-b"]["status"] == "can_use"
    # Worker A may see the connection but may not run it: it is listed with the permission it needs.
    assert by_agent["worker-a"]["status"] == "needs_permission"
    assert by_agent["worker-a"]["needs_label"] == "Run capabilities"


# ── the preview a workspace change shows before it is saved ──────────────────


def test_adding_a_workspace_is_previewed_before_anything_is_saved(world):
    client, headers = world["client"], world["headers"]
    preview = client.post(
        "/api/access/preview/agent-workspace",
        json={"agent_id": "worker-a", "workspace_id": "other", "action": "add", "read": True, "write": False},
        headers=headers,
    ).json()["data"]
    assert preview["opens_memory"] is True
    assert preview["workspace"]["name"] == "Other place"
    # Nothing is saved until the person presses Save.
    agent = client.get("/api/agents/worker-a").json()["data"]["agent"]
    assert "workspace:other" not in agent["read_scopes_json"]


def test_saving_a_workspace_keeps_the_agents_own_space_and_its_other_scopes(world):
    client, headers = world["client"], world["headers"]
    before = client.get("/api/agents/worker-a").json()["data"]["agent"]
    saved = client.put(
        "/api/agents/worker-a",
        json={"read_scopes": json.loads(before["read_scopes_json"]) + ["workspace:other"],
              "write_scopes": json.loads(before["write_scopes_json"])},
        headers=headers,
    )
    assert saved.status_code == 200, saved.text
    after = client.get("/api/agents/worker-a").json()["data"]["agent"]
    assert "workspace:other" in after["read_scopes_json"]
    assert "agent:worker-a" in after["read_scopes_json"]
    assert "workspace:acc" in after["read_scopes_json"]
