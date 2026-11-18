"""The agent's Access dialog, in a real browser: what it can do and reach, and what adding a workspace would open
before anything is saved."""

import pytest

from tests.browser.conftest import seed

SEED = '''
from app.database import get_db
from app.services import agent_service, workspace_service
with get_db() as conn:
    owner = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
workspace_service.create_workspace("main-ws", "Main Place", owner)
workspace_service.create_workspace("other-ws", "Other Place", owner)
agent_service.create_agent(agent_id="worker-a", display_name="Worker A", owner_user_id=owner,
                           read_scopes=["workspace:main-ws"], write_scopes=[], capabilities=["memory", "credentials"])
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
