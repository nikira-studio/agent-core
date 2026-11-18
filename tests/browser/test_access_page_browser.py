"""The Access page in a real browser: the table it builds from the overview, and the filter that
narrows it to one agent.

Skipped when Playwright or its browser is not installed. Uses the shared throwaway instance (conftest.py).
"""

import pytest

from tests.browser.conftest import seed

SEED = '''
import json
from app.database import get_db
from app.services import agent_service, connector_service, credential_service, workspace_service

with get_db() as conn:
    owner = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]

workspace_service.create_workspace("acc-ws", "Accounts", owner)
agent_service.create_agent(agent_id="worker-a", display_name="Worker A", owner_user_id=owner,
                           read_scopes=["workspace:acc-ws"], write_scopes=[],
                           capabilities=["memory", "credentials", "connectors_read", "connectors_execute"])
agent_service.create_agent(agent_id="worker-b", display_name="Worker B", owner_user_id=owner,
                           read_scopes=["workspace:acc-ws"], write_scopes=[],
                           capabilities=["memory", "credentials", "connectors_read", "connectors_execute"])
credential_service.create_credential("workspace:acc-ws", "seed-key", "not-shown")
entry = credential_service.create_credential("workspace:acc-ws", "signed-key", "not-shown")
connector_service.create_connector_type(
    connector_type_id="seed-cap", display_name="Seed capability", auth_type="none", provider_type="builtin",
    supported_actions=["ping"], backend_type="http",
    backend_json=json.dumps({"type": "http", "base_url": {"from": "config", "field": "base_url"}, "requests": {}}),
)
connector_service.create_binding(
    "seed-cap", "Main", "workspace:acc-ws", credential_id=entry["id"],
    config_json='{"base_url": "http://example.test"}'
)
'''


@pytest.fixture(scope="module")
def access_page(instance, admin_page):
    seed(instance, SEED)
    admin_page.goto(instance + "/access")
    admin_page.wait_for_load_state("load")
    admin_page.wait_for_selector("#access-rows tr")
    return admin_page


def test_the_page_lists_what_reaches_what(access_page):
    page = access_page
    assert page.locator("main h1").all_inner_texts() == ["Access"]
    rows = page.locator("#access-rows tr")
    assert rows.count() >= 2
    text = page.locator("#access-rows").text_content()
    assert "Worker A" in text and "Worker B" in text
    assert "seed-key" in text
    # the sentence the server words for each kind of assignment
    assert "An agent can reach a credential" in text
    count = page.locator("#access-row-count").text_content()
    assert count in (f"{rows.count()} rows", f"{rows.count()} row")


def test_the_agent_filter_keeps_only_that_agents_rows(access_page):
    page = access_page
    total = page.locator("#access-rows tr").count()
    page.select_option("#access-filter-agent", "Worker A")
    page.wait_for_function(f"document.querySelectorAll('#access-rows tr').length < {total}")
    text = page.locator("#access-rows").text_content()
    assert "Worker A" in text
    assert "Worker B" not in text
    # the filter is put back, and every row comes back with it
    page.select_option("#access-filter-agent", "")
    page.wait_for_function(f"document.querySelectorAll('#access-rows tr').length === {total}")


def test_the_unused_list_is_under_its_own_heading(access_page):
    page = access_page
    assert "Unused" in page.locator("main h2").all_inner_texts()
    assert "Things nothing uses. Check each before you delete it." in page.locator("main").text_content()


def test_both_cards_say_loading_while_the_answer_is_on_its_way(instance, admin_page):
    page = admin_page
    release = []

    def slow(route):
        release.append(route)  # hold the answer until the test lets it go

    page.route("**/api/access/overview", slow)
    try:
        page.goto(instance + "/access")
        page.wait_for_load_state("load")
        assert page.locator("#access-loading").is_visible()
        assert page.locator("#access-loading").text_content().strip() == "Loading..."
        assert page.locator("#access-unused-loading").is_visible()
        assert not page.locator("#access-table-wrap").is_visible()  # no bare table headers
        while not release:
            page.wait_for_timeout(50)
        release[0].continue_()
        page.wait_for_selector("#access-rows tr")
        assert not page.locator("#access-loading").is_visible()
        assert not page.locator("#access-unused-loading").is_visible()
        assert page.locator("#access-table-wrap").is_visible()
    finally:
        page.unroute("**/api/access/overview")


def test_a_failed_answer_replaces_loading_with_the_error(instance, admin_page):
    page = admin_page
    page.route("**/api/access/overview", lambda route: route.fulfill(
        status=500, content_type="application/json",
        body='{"ok": false, "error": {"code": "BOOM", "message": "The access list could not be loaded"}}'))
    try:
        page.goto(instance + "/access")
        page.wait_for_function(
            "document.getElementById('access-loading').textContent.includes('could not be loaded')"
        )
        assert "could not be loaded" in page.locator("#access-unused-loading").text_content()
        assert not page.locator("#access-table-wrap").is_visible()
    finally:
        page.unroute("**/api/access/overview")
