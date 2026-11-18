"""The agent's Access dialog, in a real browser: what it can do and reach, and what adding a workspace would open
before anything is saved."""

import pytest

from tests.browser.conftest import seed

SEED = '''
from app.database import get_db
from app.services import agent_service, credential_service, workspace_service
with get_db() as conn:
    owner = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
workspace_service.create_workspace("main-ws", "Main Place", owner)
workspace_service.create_workspace("other-ws", "Other Place", owner)
agent_service.create_agent(agent_id="worker-a", display_name="Worker A", owner_user_id=owner,
                           read_scopes=["workspace:main-ws"], write_scopes=[], capabilities=["memory", "credentials"])
# Worker B cannot reference a credential yet: it has no Reference credentials permission.
agent_service.create_agent(agent_id="worker-b", display_name="Worker B", owner_user_id=owner,
                           read_scopes=["workspace:main-ws"], write_scopes=[], capabilities=["memory"])
credential_service.create_credential("workspace:main-ws", "shared-key", "not-a-real-value")
'''


@pytest.fixture(scope="module")
def agents(instance, admin_page):
    seed(instance, SEED)
    admin_page.goto(instance + "/agents")
    admin_page.wait_for_load_state("load")
    return admin_page


def _open_access(page):
    page.locator("tr:has([data-agent-edit='worker-a']) summary").click()
    page.locator("[data-agent-access='worker-a']").click()
    page.wait_for_selector("#access-agent-modal .modal", state="visible")


def _stored_scopes(page):
    return page.evaluate(
        "async () => (await apiFetch('/api/agents/worker-a')).data.agent.read_scopes_json"
    )


def test_the_dialog_says_what_the_agent_can_do_and_reach(agents):
    page = agents
    _open_access(page)
    dialog = page.locator("#access-agent-modal")
    text = dialog.text_content()  # not inner_text: headings are shown in capitals by CSS
    assert "What Worker A can do and reach" in text
    assert "Workspaces" in text and "Main Place" in text
    assert "reads" in text
    assert "Capabilities" in text
    assert "Credentials it can reference" in text
    assert "Keys" in text and "Permissions" in text
    assert "Add, rename or revoke keys under More > Keys." in text
    assert "workspace:" not in text  # plain words, not scope syntax
    page.locator("#access-agent-cancel").click()


def test_the_pickers_ask_which_one_before_anything_is_added(agents):
    """Every Add picker starts on "Choose...", and its Add button opens only once something is chosen.

    This runs before the workspace is added below, so there is still a workspace to choose."""
    page = agents
    _open_access(page)
    for pick_id, add_id in (
        ("#access-agent-workspace-pick", "#access-agent-workspace-add"),
        ("#access-agent-capability-pick", "#access-agent-capability-add"),
    ):
        pick = page.locator(pick_id)
        assert pick.locator("option").first.text_content() == "Choose..."
        assert page.locator(add_id).is_disabled()
    # Choosing a workspace opens the Add button.
    page.locator("#access-agent-workspace-pick").select_option(label="Other Place")
    assert not page.locator("#access-agent-workspace-add").is_disabled()
    page.locator("#access-agent-cancel").click()


def test_adding_a_workspace_is_previewed_before_it_is_saved(agents):
    page = agents
    _open_access(page)
    page.locator("#access-agent-workspace-pick").select_option(label="Other Place")
    page.locator("#access-agent-workspace-add").click()
    page.wait_for_function(
        "document.querySelector('#access-agent-workspace-preview').innerText.includes('opens its memory')"
    )
    preview = page.locator("#access-agent-workspace-preview").inner_text()
    assert "Adding Other Place opens its memory." in preview
    assert "Credentials in it:" in preview and "Connections in it:" in preview
    # Nothing is saved until Save is pressed.
    assert "workspace:other-ws" not in _stored_scopes(page)

    page.locator("#access-agent-save").click()
    page.wait_for_function(
        "async () => (await apiFetch('/api/agents/worker-a')).data.agent.read_scopes_json.includes('workspace:other-ws')"
    )
    assert "workspace:other-ws" in _stored_scopes(page)
    page.locator("#access-agent-cancel").click()


def test_a_credential_that_needs_a_permission_says_it_once(agents):
    """The list says once what it is missing, with one button; the row itself has no button."""
    page = agents
    page.locator("tr:has([data-agent-edit='worker-b']) summary").click()
    page.locator("[data-agent-access='worker-b']").click()
    page.wait_for_selector("#access-agent-modal .modal", state="visible")
    page.wait_for_function(
        "document.querySelector('#access-agent-credentials').innerText.includes('shared-key')"
    )
    credentials = page.locator("#access-agent-credentials")
    text = credentials.inner_text()
    # The owner is named once: the reason already names it.
    assert text.count("workspace Main Place") == 1
    assert "through workspace Main Place" in text
    # One notice, one button, and the row keeps a quiet note of its own.
    # One row needs it, so the notice says "this one", not "some of these".
    assert "This needs the Reference credentials permission before this agent can use it." in text
    assert credentials.locator("button").count() == 1
    assert "Turn on Reference credentials" in text
    assert "needs permission" in text
    page.locator("#access-agent-cancel").click()


def test_the_one_button_turns_the_permission_on_and_the_notice_goes(agents):
    page = agents
    page.locator("tr:has([data-agent-edit='worker-b']) summary").click()
    page.locator("[data-agent-access='worker-b']").click()
    page.wait_for_selector("#access-agent-modal .modal", state="visible")
    page.wait_for_function(
        "document.querySelector('#access-agent-credentials').innerText.includes('shared-key')"
    )
    page.locator("#access-agent-credentials button", has_text="Turn on Reference credentials").click()
    page.wait_for_function(
        "document.querySelector('#access-agent-credentials').innerText.includes('can use its reference')"
    )
    text = page.locator("#access-agent-credentials").inner_text()
    assert "Some of these need the" not in text
    assert "needs permission" not in text
    page.locator("#access-agent-cancel").click()
