"""A connection's "Who can use it", in a real browser: an agent is put on the list through the preview, and the list of
the agents that use it follows."""

import pytest

from tests.browser.conftest import seed

SEED = '''
import json
from app.database import get_db
from app.services import agent_service, connector_service as cs, workspace_service
with get_db() as conn:
    owner = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
workspace_service.create_workspace("main-ws", "Main Place", owner)
agent_service.create_agent(agent_id="worker-b", display_name="Worker B", owner_user_id=owner,
                           capabilities=["memory", "connectors_read", "connectors_execute"])
cs.create_connector_type("ledger-cap", "Ledger capability", auth_type="none", provider_type="builtin",
                          supported_actions=[{"name": "ping", "side_effect": "read", "description": "Ping"}],
                          backend_type="http",
                          backend_json=json.dumps({"type": "http", "requests": {"ping": {"method": "GET", "path": "/ping"}}}))
cs.create_binding("ledger-cap", "Ledger connection", "workspace:main-ws", config_json='{"base_url": "http://example.test"}')
'''


@pytest.fixture(scope="module")
def connections(instance, admin_page):
    seed(instance, SEED)
    admin_page.goto(instance + "/connectors")
    admin_page.wait_for_load_state("load")
    return admin_page


def _open_dialog(page):
    page.locator("tr[data-binding-id]", has_text="Ledger connection").locator("[data-binding-users]").click()
    page.wait_for_selector("#access-connection-modal .modal", state="visible")


def test_adding_an_agent_is_previewed_and_then_saved(connections):
    page = connections
    _open_dialog(page)
    dialog = page.locator("#access-connection-modal")
    # text_content, not inner_text: headings are shown in capitals by CSS.
    assert "Who can use it" in dialog.text_content()

    page.locator("#access-connection-add-kind").select_option("agent")
    page.locator("#access-connection-add-ref").select_option(label="Worker B")
    page.locator("#access-connection-add").click()
    page.wait_for_function(
        "document.querySelector('#access-connection-add-preview').innerText.includes('Agents that gain it')"
    )
    preview = page.locator("#access-connection-add-preview").inner_text()
    assert "Worker B" in preview
    assert "Agents that lose it" in preview
    # Nothing is saved until Save is pressed.
    assert "Worker B" not in page.locator("#access-connection-users").inner_text()

    page.locator("#access-connection-add-preview").get_by_role("button", name="Save").click()
    page.wait_for_function(
        "document.querySelector('#access-connection-users').innerText.includes('Worker B')"
    )
    users = page.locator("#access-connection-users").inner_text()
    assert "Worker B" in users and "named on it" in users
    # The list a person edits shows the entry too.
    assert "Worker B" in page.locator("#access-connection-allowed").inner_text()
