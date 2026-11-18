"""The Access page in a real browser: the table it builds from the overview, and the filter that
narrows it to one agent.

Skipped when Playwright or its browser is not installed. Uses the shared throwaway instance (conftest.py).
"""

import pytest

from tests.browser.conftest import seed, sign_in

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
    rows = page.locator("#access-rows tr.access-group-row")
    assert rows.count() >= 2
    text = page.locator("#access-rows").text_content()
    assert "Worker A" in text and "Worker B" in text
    assert "seed-key" in text
    # the sentence the server words for each kind of assignment
    assert "An agent can reach a credential" in text
    groups = page.locator("#access-rows tr.access-group-head").count()
    count = page.locator("#access-row-count").text_content()
    assert count == f"{rows.count()} rows in {groups} groups"


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


CONTROLS_OUTSIDE_THEIR_CARD = """
() => {
  const bad = [];
  document.querySelectorAll('.card select, .card input').forEach(function (el) {
    const card = el.closest('.card');
    if (!card) return;
    const c = card.getBoundingClientRect();
    const e = el.getBoundingClientRect();
    if (e.right > c.right + 1 || e.left < c.left - 1) {
      bad.push(el.id + ' (' + Math.round(e.left) + '-' + Math.round(e.right) + ') outside ('
        + Math.round(c.left) + '-' + Math.round(c.right) + ')');
    }
  });
  return bad;
}
"""


def test_the_page_fits_a_phone(browser, instance, access_page):
    """A narrow phone: the filter bar must not push the page sideways, and every filter stays in its card."""
    context = browser.new_context(viewport={"width": 390, "height": 844})
    page = context.new_page()
    try:
        sign_in(page, instance, "owner@example.com")
        page.goto(instance + "/access")
        page.wait_for_load_state("load")
        page.wait_for_selector("#access-rows tr")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        # Each filter ends inside the card that holds it.
        edges = page.evaluate(
            """
            () => {
              const card = document.querySelector('#access-rows').closest('.card');
              const right = card.getBoundingClientRect().right;
              return Array.from(document.querySelectorAll(
                '#access-show, #access-filter-name, #access-filter-agent, '
                + '#access-filter-capability, #access-filter-service'
              )).map(function (el) {
                return { id: el.id, right: el.getBoundingClientRect().right, cardRight: right };
              });
            }
            """
        )
        assert edges, "the page must have filters"
        for edge in edges:
            assert edge["right"] <= edge["cardRight"] + 1, edge
    finally:
        context.close()


def test_the_other_pages_keep_their_controls_inside_their_cards(browser, instance, access_page):
    """The same fault on the other pages: a select in a card must not run past the card's edge."""
    context = browser.new_context(viewport={"width": 390, "height": 844})
    page = context.new_page()
    try:
        sign_in(page, instance, "owner@example.com")
        for path in ("/credentials", "/connectors"):
            page.goto(instance + path)
            page.wait_for_load_state("load")
            outside = page.evaluate(CONTROLS_OUTSIDE_THEIR_CARD)
            assert not outside, (path, outside)
    finally:
        context.close()


# ---- the rows are grouped under one header for each "Who or what" -----------------------------------------------

@pytest.fixture
def grouped_page(instance, access_page):
    """The page loaded afresh: an earlier test may have left it on a held or failed answer."""
    page = access_page
    page.goto(instance + "/access")
    page.wait_for_load_state("load")
    page.wait_for_selector("#access-rows tr.access-group-head")
    return page


def _open_headers(page):
    return page.locator("#access-rows .access-group-toggle[aria-expanded=true]").count()


def _visible_rows(page):
    return page.locator("#access-rows tr.access-group-row:visible").count()


def _reset(page):
    page.select_option("#access-show", "")
    page.fill("#access-filter-name", "")
    page.select_option("#access-filter-agent", "")
    page.wait_for_function("document.querySelectorAll('#access-rows .access-group-toggle[aria-expanded=true]').length === 0")


def test_every_group_is_closed_at_first_and_its_header_says_who_what_kind_and_how_many(grouped_page):
    page = grouped_page
    _reset(page)
    assert page.locator("#access-rows tr.access-group-head").count() >= 2
    assert _open_headers(page) == 0 and _visible_rows(page) == 0
    head = page.locator("#access-rows .access-group-toggle", has_text="Worker A").first
    text = head.text_content()
    assert "Worker A" in text and "agent" in text and "rows" in text or "row" in text
    assert head.get_attribute("aria-expanded") == "false"
    # the Unused card is still the first card on the page
    first_card = page.locator("main .card").first
    assert "Unused" in first_card.locator("h2").first.text_content()


def test_a_header_opens_and_closes_its_rows_by_click_and_by_keyboard(grouped_page):
    page = grouped_page
    _reset(page)
    head = page.locator("#access-rows .access-group-toggle", has_text="Worker A").first
    head.click()
    assert head.get_attribute("aria-expanded") == "true" and _visible_rows(page) >= 1
    head.click()
    assert head.get_attribute("aria-expanded") == "false" and _visible_rows(page) == 0
    head.focus()
    page.keyboard.press("Enter")
    assert head.get_attribute("aria-expanded") == "true" and _visible_rows(page) >= 1
    page.keyboard.press("Space")
    assert head.get_attribute("aria-expanded") == "false" and _visible_rows(page) == 0


def test_a_filter_finds_rows_inside_closed_groups_and_opens_the_groups_that_match(grouped_page):
    page = grouped_page
    _reset(page)
    page.fill("#access-filter-name", "seed-key")
    page.wait_for_function("document.querySelectorAll('#access-rows .access-group-toggle[aria-expanded=true]').length > 0")
    visible = page.locator("#access-rows tr.access-group-row:visible").all_text_contents()
    assert visible and all("seed-key" in text for text in visible)
    # a group a person closed by hand stays closed until the filter changes again
    while _open_headers(page):
        page.locator("#access-rows .access-group-toggle[aria-expanded=true]").first.click()
    assert _visible_rows(page) == 0
    page.fill("#access-filter-name", "seed-ke")
    page.wait_for_function("document.querySelectorAll('#access-rows tr.access-group-row:not(.is-closed)').length > 0")
    # clearing the filter puts every group back to closed
    page.fill("#access-filter-name", "")
    page.wait_for_function("document.querySelectorAll('#access-rows .access-group-toggle[aria-expanded=true]').length === 0")


def test_the_groups_work_on_a_phone(browser, instance, access_page):
    context = browser.new_context(viewport={"width": 390, "height": 844})
    page = context.new_page()
    try:
        sign_in(page, instance, "owner@example.com")
        page.goto(instance + "/access")
        page.wait_for_load_state("load")
        page.wait_for_selector("#access-rows tr.access-group-head")
        assert _visible_rows(page) == 0
        head = page.locator("#access-rows .access-group-toggle").first
        box = head.bounding_box()
        assert box["x"] >= 0 and box["x"] + box["width"] <= 391 and box["height"] >= 40
        head.click()
        assert _visible_rows(page) >= 1
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    finally:
        context.close()


# ---- the header row is the control, and the columns keep their places -------

def test_the_header_row_opens_the_group_when_it_is_clicked(grouped_page):
    """The whole header row is the control: a click on the row opens the group, and the button does
    not toggle it twice."""
    page = grouped_page
    _reset(page)
    head = page.locator("#access-rows tr.access-group-head").first
    button = head.locator(".access-group-toggle").first
    assert button.get_attribute("aria-expanded") == "false"
    # A click on the row itself, not on the button.
    head.evaluate("el => el.dispatchEvent(new MouseEvent('click', {bubbles: true}))")
    assert button.get_attribute("aria-expanded") == "true"
    assert _visible_rows(page) >= 1
    # A click on the button opens and closes it, once each time.
    button.click()
    assert button.get_attribute("aria-expanded") == "false"
    assert _visible_rows(page) == 0
    button.click()
    assert button.get_attribute("aria-expanded") == "true"


def test_the_name_is_in_the_header_and_not_on_every_row(grouped_page):
    """The header names the group; the rows under it do not repeat the name."""
    page = grouped_page
    _reset(page)
    page.locator("#access-rows .access-group-toggle", has_text="Worker A").first.click()
    row = page.locator("#access-rows tr.access-group-row:not(.is-closed)").first
    # The first cell is empty: the columns still line up.
    assert row.locator("td").first.inner_text().strip() == ""
    # The row is found by what it reaches.
    assert row.locator("td").nth(1).inner_text().strip() != ""


def test_the_columns_keep_their_places_when_a_group_opens(browser, instance, access_page):
    """At a wide window the Reaches column stays where it was: opening a group moves nothing."""
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    page = context.new_page()
    try:
        sign_in(page, instance, "owner@example.com")
        page.goto(instance + "/access")
        page.wait_for_load_state("load")
        page.wait_for_selector("#access-rows tr.access-group-head")
        before = page.evaluate(
            "document.querySelector('table.access-table thead th:nth-child(2)').getBoundingClientRect().left"
        )
        page.locator("#access-rows .access-group-toggle").first.click()
        page.wait_for_selector("#access-rows tr.access-group-row:not(.is-closed)")
        after = page.evaluate(
            "document.querySelector('table.access-table thead th:nth-child(2)').getBoundingClientRect().left"
        )
        assert abs(after - before) <= 1, (before, after)
        # The first open row's Reaches value starts under the heading.
        cell = page.evaluate(
            """
            () => {
              const row = document.querySelector('#access-rows tr.access-group-row:not(.is-closed)');
              if (!row) return null;
              return {
                cell: row.children[1].getBoundingClientRect().left,
                head: document.querySelector('table.access-table thead th:nth-child(2)').getBoundingClientRect().left,
              };
            }
            """
        )
        assert cell is not None
        assert abs(cell["cell"] - cell["head"]) <= 1, cell
    finally:
        context.close()
