"""Credentials > Used by and the Access dialog, in a real browser: the cell says who was given the secret, and
the dialog is one list of who can use the credential."""

import pytest

from tests.browser.conftest import seed

SEED = '''
from app.database import get_db
from app.services import agent_service, credential_service, workspace_service
with get_db() as conn:
    owner = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
workspace_service.create_workspace("shared-ws", "Shared Place", owner)
for agent_id, name, caps in (
    ("reader-one", "Reader One", ["memory", "credentials"]),
    ("reader-two", "Reader Two", ["memory", "credentials"]),
):
    agent_service.create_agent(agent_id=agent_id, display_name=name, owner_user_id=owner,
                               read_scopes=["workspace:shared-ws"], write_scopes=[], capabilities=caps)
agent_service.create_agent(agent_id="worker-a", display_name="Worker A", owner_user_id=owner, kind="service")
credential_service.create_credential("workspace:shared-ws", "shared_token", "not-a-real-value")
'''


@pytest.fixture(scope="module")
def credentials(instance, admin_page):
    seed(instance, SEED)
    admin_page.goto(instance + "/credentials")
    admin_page.wait_for_load_state("load")
    return admin_page


def _cell(page):
    return page.locator("tr[data-credential-id] td[data-label='Used by']").first


def test_the_cell_says_who_was_given_the_secret_and_who_else_can_see_its_name(credentials):
    page = credentials
    before = _cell(page).inner_text()
    assert "Given the secret: no one" in before and "2 others can see its name" in before
    assert "can get its reference" not in before


def test_the_dialog_is_one_list_in_three_groups_and_a_service_can_be_given_the_secret(credentials):
    page = credentials
    page.evaluate("window.__stillHere = true")
    page.locator("[data-credential-access]").first.click()
    page.wait_for_selector("#access-modal .modal", state="visible")
    text = page.locator("#access-modal").text_content()  # the headings may be styled in capitals
    assert "Capabilities that sign in with this" in text
    assert "Agents that can reach this" in text
    assert "Services that receive this value" in text
    assert "A service receives the secret value itself. An agent using a capability does not." in text
    assert "Change owner" in text

    agents = page.locator("#access-agents").inner_text()
    assert "Reader One" in agents and "Reader Two" in agents
    assert "through workspace Shared Place" in agents
    assert "can use its reference" in agents
    assert "workspace:" not in agents  # plain words, not scope syntax

    services = page.locator("#access-services").inner_text()
    assert "No service receives this yet." in services

    # a service is given the secret from the list, and the row in the table follows without a reload
    page.locator("#access-service-pick").select_option(label="Worker A")
    page.locator("#access-service-add").click()
    page.wait_for_function("document.querySelector('#access-services').innerText.includes('Worker A')")
    services = page.locator("#access-services").inner_text()
    assert "read only" in services and "no end date" in services
    assert "Given the secret: Worker A" in _cell(page).inner_text()
    assert page.evaluate("window.__stillHere") is True  # no reload

    page.locator("#access-services button", has_text="Remove").click()
    page.wait_for_function("document.querySelector(\"tr[data-credential-id] td[data-label='Used by']\").innerText.includes('no one')")
    assert "2 others can see its name" in _cell(page).inner_text()
    assert page.evaluate("window.__stillHere") is True  # closing does not reload either, so the cell is already right
