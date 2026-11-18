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
    # Reader Three may reach the credential but may not use it yet.
    ("reader-three", "Reader Three", ["memory"]),
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


def test_the_cell_says_who_was_given_the_secret_and_who_else_can_reach_it(credentials):
    page = credentials
    before = _cell(page).inner_text()
    assert "Given the secret: no one" in before and "3 agents can reach it" in before
    assert "can get its reference" not in before


def test_the_dialog_is_one_list_in_three_groups_and_a_service_can_be_given_the_secret(credentials):
    page = credentials
    page.evaluate("window.__stillHere = true")
    page.locator("[data-credential-access]").first.click()
    page.wait_for_selector("#access-modal .modal", state="visible")
    text = page.locator("#access-modal").text_content()  # the headings may be styled in capitals
    assert "Connections that sign in with this" in text
    assert "Agents that can reach this" in text
    assert "Services that receive this value" in text
    assert "A service receives the secret value itself. An agent using a capability does not." in text
    assert "Change owner" in text

    # the lists are filled after the dialog opens, from the server: wait for them rather than reading at once
    page.wait_for_function("document.querySelector('#access-agents').innerText.includes('Reader Two')")
    page.wait_for_function("document.querySelector('#access-services').innerText.includes('No service receives this yet.')")
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
    # the table cell is refreshed after the dialog's own lists (a second request): wait for it, do not read at once
    page.wait_for_function("document.querySelector(\"tr[data-credential-id] td[data-label='Used by']\").innerText.includes('Worker A')")
    assert "Given the secret: Worker A" in _cell(page).inner_text()
    assert page.evaluate("window.__stillHere") is True  # no reload

    page.locator("#access-services button", has_text="Remove").click()
    page.wait_for_function("document.querySelector(\"tr[data-credential-id] td[data-label='Used by']\").innerText.includes('no one')")
    assert "3 agents can reach it" in _cell(page).inner_text()
    assert page.evaluate("window.__stillHere") is True  # closing does not reload either, so the cell is already right


def test_the_pickers_ask_which_one_before_anything_is_added(credentials):
    """Every Add picker starts on "Choose...", and its Add button opens only once something is chosen."""
    page = credentials
    page.reload()  # an earlier test leaves its dialog open
    page.wait_for_load_state("load")
    page.locator("[data-credential-access]").first.click()
    page.wait_for_selector("#access-modal .modal", state="visible")
    page.wait_for_function("document.querySelector('#access-services').innerText.includes('No service receives this yet.')")
    for pick_id, add_id in (
        ("#access-connection-pick", "#access-connection-add"),
        ("#access-service-pick", "#access-service-add"),
    ):
        pick = page.locator(pick_id)
        assert pick.locator("option").first.text_content() == "Choose..."
        assert page.locator(add_id).is_disabled()
    # Choosing a service opens the Add button.
    page.locator("#access-service-pick").select_option(label="Worker A")
    assert not page.locator("#access-service-add").is_disabled()


def test_the_agents_group_says_once_what_it_is_missing(credentials):
    """One notice and one Turn on at the top of the group; the rows have no buttons of their own."""
    page = credentials
    page.reload()
    page.wait_for_load_state("load")
    page.locator("[data-credential-access]").first.click()
    page.wait_for_selector("#access-modal .modal", state="visible")
    page.wait_for_function("document.querySelector('#access-agents').innerText.includes('Reader Three')")
    agents = page.locator("#access-agents")
    text = agents.inner_text()
    # One row of the three needs it, so the notice names that agent, not "some of these".
    assert "Reader Three needs the Reference credentials permission before it can use it." in text
    assert "Turn on Reference credentials" in text
    assert agents.locator("button").count() == 1
    assert "needs permission" in text


def test_the_owner_panel_is_quiet_until_the_owner_changes(credentials):
    """The panel opens on the owner it has now: nothing changes, and Save is off."""
    page = credentials
    page.reload()
    page.wait_for_load_state("load")
    page.locator("[data-credential-access]").first.click()
    page.wait_for_selector("#access-modal .modal", state="visible")
    page.locator("#access-owner-change").click()
    page.wait_for_function("document.querySelector('#access-owner-preview').innerText.includes('Nothing changes.')")
    assert page.locator("#access-owner-save").is_disabled()
    # The panel is on the screen, not hidden behind the dialog's footer.
    panel = page.locator("#access-owner-panel")
    box = panel.bounding_box()
    footer = page.locator("#access-modal .modal-footer").bounding_box()
    assert box is not None and footer is not None
    assert box["y"] + box["height"] <= footer["y"] + 1, (box, footer)
    # Choosing another owner turns Save on; choosing this one again turns it off.
    page.locator("#access-owner-pick").select_option(label="Just me (personal)")
    page.wait_for_function("!document.getElementById('access-owner-save').disabled")
    page.locator("#access-owner-pick").select_option(label="Shared Place")
    page.wait_for_function("document.getElementById('access-owner-save').disabled")
