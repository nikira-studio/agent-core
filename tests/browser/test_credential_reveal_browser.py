"""Showing a stored value, in a real browser: nothing is in the page until Show value is pressed, the value goes away
on Hide, after 30 seconds and when the dialog closes, and the audit log records each time."""

import pytest

from tests.browser.conftest import seed

VALUE = "browser-reveal-secret-9f3"

SEED = f'''
from app.services import credential_service, workspace_service
from app.database import get_db
with get_db() as conn:
    owner = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
workspace_service.create_workspace("reveal-ws", "Reveal Place", owner)
credential_service.create_credential("workspace:reveal-ws", "reveal_token", "{VALUE}")
'''


@pytest.fixture(scope="module")
def credentials(instance, admin_page):
    seed(instance, SEED)
    admin_page.goto(instance + "/credentials")
    admin_page.wait_for_load_state("load")
    return admin_page


def _open_edit(page):
    page.reload()
    page.wait_for_load_state("load")
    page.locator("[data-credential-edit]").first.click()
    page.wait_for_selector("#edit-credential-modal .modal", state="visible")
    page.wait_for_function("document.getElementById('edit-credential-name').value !== ''")


def _shown(page):
    return page.locator("#edit-credential-revealed-value").text_content()


def test_nothing_is_in_the_page_until_show_value_is_pressed(credentials):
    page = credentials
    assert VALUE not in page.content()
    _open_edit(page)
    assert VALUE not in page.content()
    assert page.locator("#edit-credential-revealed").is_hidden()
    assert page.locator("#edit-credential-reveal").is_visible()
    # not in the table
    assert page.locator("table").locator("text=Show value").count() == 0


def test_show_value_shows_it_and_hide_takes_it_away(credentials):
    page = credentials
    _open_edit(page)
    page.locator("#edit-credential-reveal").click()
    page.wait_for_function("document.getElementById('edit-credential-revealed-value').textContent !== ''")
    assert _shown(page) == VALUE
    assert page.locator("#edit-credential-reveal-copy").is_visible()
    assert page.locator("#edit-credential-reveal").is_hidden()
    assert "Hidden again after 30 seconds" in page.locator("#edit-credential-reveal-note").text_content()
    page.locator("#edit-credential-reveal-hide").click()
    assert _shown(page) == ""
    assert VALUE not in page.content()
    assert page.locator("#edit-credential-reveal").is_visible()


def test_the_value_goes_when_the_dialog_closes(credentials):
    page = credentials
    _open_edit(page)
    page.locator("#edit-credential-reveal").click()
    page.wait_for_function("document.getElementById('edit-credential-revealed-value').textContent !== ''")
    page.locator("#edit-credential-modal button", has_text="Cancel").click()
    page.wait_for_function("document.getElementById('edit-credential-revealed-value').textContent === ''")
    assert VALUE not in page.content()
    # opening it again starts hidden
    page.locator("[data-credential-edit]").first.click()
    page.wait_for_selector("#edit-credential-modal .modal", state="visible")
    assert page.locator("#edit-credential-revealed").is_hidden()


def test_the_value_hides_itself_after_thirty_seconds(instance, admin_page):
    page = admin_page
    page.clock.install()
    page.goto(instance + "/credentials")
    page.wait_for_load_state("load")
    page.locator("[data-credential-edit]").first.click()
    page.wait_for_selector("#edit-credential-modal .modal", state="visible")
    page.wait_for_function("document.getElementById('edit-credential-name').value !== ''")
    page.locator("#edit-credential-reveal").click()
    page.wait_for_function("document.getElementById('edit-credential-revealed-value').textContent !== ''")
    page.clock.fast_forward(29000)
    assert _shown(page) == VALUE
    page.clock.fast_forward(2000)
    assert _shown(page) == ""


def test_each_time_is_recorded_without_the_value(credentials):
    page = credentials
    rows = page.evaluate(
        "async () => (await apiFetch('/api/dashboard/audit?action=credential_reveal')).data"
    )
    text = str(rows)
    assert rows and rows['events']
    assert "credential_reveal" in text and VALUE not in text
