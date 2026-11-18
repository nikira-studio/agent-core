"""A connection made with a new credential, in a real browser: the credential is made in one step and belongs to the
connection, and the Credentials page says which connection it belongs to."""

import pytest

from tests.browser.conftest import seed

SEED = '''
import json
from app.database import get_db
from app.services import connector_service as cs, workspace_service
with get_db() as conn:
    owner = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
workspace_service.create_workspace("main-ws", "Main Place", owner)
cs.create_connector_type(
    "own-cap", "Own capability", auth_type="bearer", provider_type="builtin",
    supported_actions=[{"name": "ping", "side_effect": "read", "description": "Ping"}],
    required_credential_fields=["api_key"], backend_type="http",
    backend_json=json.dumps({"type": "http", "requests": {"ping": {"method": "GET", "path": "/ping"}}}),
)
'''


@pytest.fixture(scope="module")
def page(instance, admin_page):
    seed(instance, SEED)
    admin_page.goto(instance + "/connectors")
    admin_page.wait_for_load_state("load")
    return admin_page


def test_a_credential_made_with_a_connection_belongs_to_it(page, instance):
    page.locator("[data-newbinding-type-id='own-cap']").click()
    page.wait_for_selector("#create-binding-modal", state="visible")
    page.fill("#binding-name", "Ledger connection")
    page.select_option("#binding-scope", label="Main Place")
    page.select_option("#binding-credential-mode", "new")
    page.fill("#binding-new-credential-name", "ledger_key")
    page.fill("#binding-new-credential-value", "not-a-real-value")

    # The credential belongs to the connection, and the line under the checkbox says so.
    checkbox = page.locator("#binding-new-credential-shared")
    assert not checkbox.is_checked()
    hint = page.locator("#binding-new-credential-owner-hint").inner_text()
    assert "Off: only Agent Core uses it and no agent can reach it." in hint
    assert "Main Place" in hint

    # The connection and its credential are made in one request.
    page.evaluate("window.__beforeCreate = true")
    with page.expect_response(
        lambda r: r.url.endswith("/api/connector-bindings") and r.request.method == "POST"
    ) as made:
        page.locator("#create-binding-form button[type=submit]").click()
    binding = made.value.json()["data"]["binding"]
    assert binding["credential_id"]

    # The page tests the connection and then shows itself again.
    page.wait_for_function("() => window.__beforeCreate === undefined", timeout=60000)

    page.goto(instance + "/credentials")
    page.wait_for_load_state("load")
    row = page.locator("tr[data-credential-id]", has_text="ledger_key").inner_text()
    assert "Connection Ledger connection" in row
    assert "capability:" not in row
