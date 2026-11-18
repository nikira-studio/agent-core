"""The five things a person who does not know the words "binding", "grant" or "scope" must be able to do, walked through
the real pages in a real browser on a throwaway instance: create an agent; connect an outside system; give an agent
access to a workspace; give a container a secret; find out who can reach a credential.

Each step clicks what a person would click. The only things set up beforehand (by seeding) are what the scenario starts
from in real life: the workspace, a stored secret and the outside system's entry in the catalogue."""

import pytest

from tests.browser.conftest import seed

SEED = '''
import json
from app.database import get_db
from app.services import connector_service as cs, credential_service, workspace_service
with get_db() as conn:
    owner = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
workspace_service.create_workspace("team-space", "Team space", owner)
workspace_service.create_workspace("project-space", "Project space", owner)
credential_service.create_credential("workspace:project-space", "database-password", "synthetic-value-not-a-secret")
cs.create_connector_type("ledger-cap", "Ledger", auth_type="none", provider_type="builtin",
                          supported_actions=[{"name": "ping", "side_effect": "read", "description": "Ping"}],
                          backend_type="http",
                          backend_json=json.dumps({"type": "http", "requests": {"ping": {"method": "GET", "path": "/ping"}}}))
'''


@pytest.fixture(scope="module")
def site(instance, admin_page):
    seed(instance, SEED)
    return admin_page, instance


def _stored(page, path):
    return page.evaluate("async (p) => (await apiFetch(p)).data", path)


def test_1_create_an_agent(site):
    page, base = site
    page.goto(base + "/agents")
    page.wait_for_load_state("load")
    page.get_by_role("button", name="+ Create Agent").click()
    page.wait_for_selector("#create-agent-modal .modal", state="visible")
    page.fill("#ca-id", "worker-a")
    page.fill("#ca-display-name", "Worker A")
    page.locator("#create-agent-form button[type=submit]").click()
    page.wait_for_function("document.getElementById('rotate-modal-title') && document.getElementById('rotate-modal-title').textContent.includes('Agent created')")
    page.goto(base + "/agents")
    page.wait_for_load_state("load")
    assert "Worker A" in page.locator("table").first.inner_text()
    assert _stored(page, "/api/agents/worker-a")["agent"]["kind"] == "agent"


def test_2_connect_an_outside_system(site):
    page, base = site
    page.goto(base + "/connectors")
    page.wait_for_load_state("load")
    assert "Previously called Services" not in page.inner_text("body")  # the old name is gone
    page.locator("[data-newbinding-type-id='ledger-cap']").click()
    page.wait_for_selector("#create-binding-modal .modal", state="visible")
    page.fill("#binding-name", "Team ledger")
    page.select_option("#binding-scope", label="Project space")
    page.select_option("#binding-credential-mode", "none")
    if page.locator("#binding-base-url-group").is_visible():
        page.fill("#binding-base-url", "http://example.test")
    page.locator("#create-binding-form button[type=submit]").click()
    page.wait_for_function("document.body.innerText.includes('Team ledger')", timeout=15000)
    page.goto(base + "/connectors")
    page.wait_for_load_state("load")
    assert "Team ledger" in page.locator("table").last.inner_text()
    connection = [b for b in _stored(page, "/api/connector-bindings")["bindings"] if b["name"] == "Team ledger"][0]
    assert [e["kind"] for e in connection["audience"]] == ["workspace"]  # everyone in the workspace can use it


def test_3_give_an_agent_access_to_a_workspace(site):
    page, base = site
    page.goto(base + "/agents")
    page.wait_for_load_state("load")
    page.locator("tr:has([data-agent-edit='worker-a']) summary").click()
    page.locator("[data-agent-access='worker-a']").click()
    page.wait_for_selector("#access-agent-modal .modal", state="visible")
    page.locator("#access-agent-workspace-pick").select_option(label="Project space")
    page.locator("#access-agent-workspace-add").click()
    page.wait_for_function("document.querySelector('#access-agent-workspace-preview').innerText.includes('opens its memory')")
    preview = page.locator("#access-agent-workspace-preview").inner_text()
    assert "Adding Project space opens its memory." in preview and "database-password" in preview  # says what it opens
    page.locator("#access-agent-save").click()
    page.wait_for_function(
        "async () => (await apiFetch('/api/agents/worker-a')).data.agent.read_scopes_json.includes('workspace:project-space')"
    )
    page.locator("#access-agent-cancel").click()
    # the connection that belongs to the workspace is now one this agent can use
    page.goto(base + "/agents")
    page.wait_for_load_state("load")
    page.locator("tr:has([data-agent-edit='worker-a']) summary").click()
    page.locator("[data-agent-access='worker-a']").click()
    page.wait_for_selector("#access-agent-modal .modal", state="visible")
    assert "Team ledger" in page.locator("#access-agent-modal").text_content()
    page.locator("#access-agent-cancel").click()


def test_4_give_a_container_a_secret(site):
    page, base = site
    page.goto(base + "/services")
    page.wait_for_load_state("load")
    page.get_by_role("button", name="+ Create service").click()
    page.wait_for_selector("#create-agent-modal .modal", state="visible")
    page.fill("#ca-id", "billing-runtime")
    page.fill("#ca-display-name", "Billing container")
    page.locator("#create-agent-form button[type=submit]").click()
    # the page reloads itself to the Services list: wait for the new service to appear, not for the address (it is the same)
    page.wait_for_selector("table >> text=Billing container", timeout=15000)
    assert "Billing container" in page.locator("table").first.inner_text()
    page.goto(base + "/credentials")
    page.wait_for_load_state("load")
    page.locator("[data-credential-access]").first.click()
    page.wait_for_selector("#access-modal .modal", state="visible")
    text = page.locator("#access-modal").text_content()
    assert "A service receives the secret value itself. An agent using a capability does not." in text
    page.locator("#access-service-pick").select_option(label="Billing container")
    page.locator("#access-service-add").click()
    page.wait_for_function("document.querySelector('#access-services').innerText.includes('Billing container')")
    assert "read only" in page.locator("#access-services").inner_text()
    entry = _stored(page, "/api/credentials/entries")["entries"][0]["id"]
    granted = _stored(page, "/api/access/credentials/" + entry)["services"]
    assert [s["service_id"] for s in granted] == ["billing-runtime"]


def test_5_find_out_who_can_reach_a_credential(site):
    page, base = site
    page.goto(base + "/credentials")
    page.wait_for_load_state("load")
    page.locator("[data-credential-access]").first.click()
    page.wait_for_selector("#access-modal .modal", state="visible")
    page.wait_for_function("document.querySelector('#access-agents').innerText.includes('Worker A')")
    agents = page.locator("#access-agents").inner_text()
    assert "through workspace Project space" in agents and "can use its reference" in agents
    assert "workspace:" not in agents  # plain words
    # the same question from the Access tab, as a list a person can filter
    page.goto(base + "/access")
    page.wait_for_load_state("load")
    page.wait_for_function("document.body.innerText.includes('Worker A')")
    assert "database-password" in page.inner_text("body")
