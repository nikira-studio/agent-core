"""Credentials > Used by and the Access dialog, in a real browser: the same two groups under the same labels, and the
row follows the dialog without a reload."""

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
    ("holder", "Holder", ["memory", "credentials", "credentials_resolve"]),
):
    agent_service.create_agent(agent_id=agent_id, display_name=name, owner_user_id=owner,
                               read_scopes=["workspace:shared-ws"], write_scopes=[], capabilities=caps)
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


def test_the_cell_and_the_dialog_say_the_same_two_things_and_the_row_follows_without_a_reload(credentials):
    page = credentials
    before = _cell(page).inner_text()
    assert "Given the secret: no one" in before and "3 others can see its name" in before
    assert "can get its reference" not in before

    page.evaluate("window.__stillHere = true")
    page.locator("[data-credential-access]").first.click()
    page.wait_for_selector("#access-modal .modal", state="visible")
    modal = page.locator("#access-modal")
    text = modal.text_content()  # the headings may be styled in capitals
    assert "Given the secret" in text and "Others who can see its name" in text
    assert "Reader One" in page.locator("#access-scope-readers").inner_text()
    assert "workspace:" not in page.locator("#access-scope-readers").inner_text()  # plain words, not scope syntax

    page.locator("#access-agent").select_option(label="Holder")
    page.locator("#access-add").click()
    page.wait_for_function("document.querySelector(\"tr[data-credential-id] td[data-label='Used by']\").innerText.includes('Holder')")
    after = _cell(page).inner_text()
    assert "Given the secret: Holder" in after and "2 others can see its name" in after  # the cell changed, the fact stayed
    assert "Holder" not in page.locator("#access-scope-readers").inner_text()
    assert page.evaluate("window.__stillHere") is True  # no reload

    page.locator("#access-grants button", has_text="Remove").click()
    page.wait_for_function("document.querySelector(\"tr[data-credential-id] td[data-label='Used by']\").innerText.includes('no one')")
    assert "3 others can see its name" in _cell(page).inner_text()
    page.keyboard.press("Escape")
    assert page.evaluate("window.__stillHere") is True  # closing does not reload either, so the cell is already right
