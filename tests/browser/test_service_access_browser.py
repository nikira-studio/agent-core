"""A service's Access dialog, in a real browser: what it can fetch, and the picker that asks which secret first."""

import pytest

from tests.browser.conftest import seed

SEED = '''
from app.database import get_db
from app.services import agent_service, credential_service, workspace_service
with get_db() as conn:
    owner = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
workspace_service.create_workspace("secret-ws", "Secret Place", owner)
credential_service.create_credential("workspace:secret-ws", "api_token", "not-a-real-value")
agent_service.create_agent(agent_id="worker-a", display_name="Worker A", owner_user_id=owner, kind="service")
'''


@pytest.fixture(scope="module")
def services(instance, admin_page):
    seed(instance, SEED)
    admin_page.goto(instance + "/services")
    admin_page.wait_for_load_state("load")
    return admin_page


def _open_access(page):
    page.locator("tr:has([data-agent-edit='worker-a']) summary").click()
    page.locator("[data-service-access='worker-a']").click()
    page.wait_for_selector("#access-service-modal .modal", state="visible")


def test_the_dialog_says_what_the_service_can_fetch(services):
    page = services
    _open_access(page)
    text = page.locator("#access-service-modal").text_content()
    assert "What Worker A can fetch" in text
    assert "Secrets it can fetch" in text
    assert "Add a secret" in text
    assert "workspace:" not in text  # plain words, not scope syntax


def test_the_picker_asks_which_secret_before_anything_is_added(services):
    page = services
    page.reload()  # the first test leaves its dialog open
    page.wait_for_load_state("load")
    _open_access(page)
    pick = page.locator("#access-service-secret-pick")
    assert pick.locator("option").first.text_content() == "Choose..."
    assert page.locator("#access-service-secret-add").is_disabled()
    # The second option is the first secret there is.
    pick.select_option(index=1)
    assert not page.locator("#access-service-secret-add").is_disabled()
