"""A phone at 390 px: the pages whose tables used to run off the side.

Needs you, Activity, Approvals, Access, Audit Log and Workspaces all show a list of things. At phone
width each row becomes a block: every cell sits on its own line with its label before the value, and the
button a person presses is inside the screen.

The browser tests cannot run in every sandbox; when Playwright is missing these are skipped.
"""

import pytest

from tests.browser.conftest import seed, sign_in

SEED = '''
import json
from app.database import get_db
from app.services import (
    activity_service, agent_service, approval_service, audit_service, connector_service,
    credential_service, workspace_service,
)

with get_db() as conn:
    owner = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]

workspace_service.create_workspace("phone-ws", "Phone Place", owner)
agent_service.create_agent(agent_id="phone-agent", display_name="Phone Agent", owner_user_id=owner,
                           read_scopes=["workspace:phone-ws", "agent:phone-agent"],
                           write_scopes=["workspace:phone-ws", "agent:phone-agent"],
                           capabilities=["memory", "coordination", "credentials", "connectors_read",
                                         "connectors_execute", "admin_propose"])
agent_service.create_agent(agent_id="phone-target", display_name="Phone Target", owner_user_id=owner,
                           read_scopes=["workspace:phone-ws"], write_scopes=[], capabilities=["memory"])

# A question that waits on a person: the Needs you page and the Activity page both list it.
ask = activity_service.create_activity(agent_id="phone-agent", user_id=owner,
                                       task_description="Check the layout on a phone",
                                       memory_scope="workspace:phone-ws")
activity_service.set_handoff(ask["id"], {"needs_human": "Please look at the layout on a phone."})
activity_service.update_activity(ask["id"], status="blocked", task_result="Waiting on a decision")

# Something to reach: a credential, a connection and the agent that may use them.
entry = credential_service.create_credential("workspace:phone-ws", "phone-key", "not-a-real-value")
connector_service.create_connector_type(
    connector_type_id="phone-cap", display_name="Phone capability", auth_type="none", provider_type="builtin",
    supported_actions=["ping"], backend_type="http",
    backend_json=json.dumps({"type": "http", "base_url": {"from": "config", "field": "base_url"}, "requests": {}}),
)
connector_service.create_binding(
    "phone-cap", "Phone connection", "workspace:phone-ws", credential_id=entry["id"],
    config_json='{"base_url": "http://example.test"}'
)

# A proposal that waits for a decision: the Approvals page lists it.
approval_service.propose(
    agent_id="phone-agent", scope="workspace:phone-ws", title="Widen the phone layout",
    operations=[{"name": "agent.update", "params": {
        "agent_id": "phone-target", "display_name": "Phone Target", "description": "Wider on a phone",
        "read_scopes": ["workspace:phone-ws"], "write_scopes": [], "capabilities": ["memory"],
    }}],
)

# An audit row or two: the Audit Log page has something to show.
audit_service.write_event(
    actor_type="user", actor_id=owner, action="workspace_created",
    resource_type="workspace", resource_id="phone-ws", result="success",
    details={"note": "a workspace to look at on a phone"},
)
'''

PHONE = {"width": 390, "height": 844}


@pytest.fixture(scope="module")
def phone(instance, browser):
    context = browser.new_context(viewport=PHONE)
    page = context.new_page()
    sign_in(page, instance, "owner@example.com")  # the first sign-in creates the owner the seed needs
    seed(instance, SEED)
    yield page
    context.close()


def _first_row_action(page, row_selector):
    """The thing a person presses in the first row: the button, or the link when there is no button."""
    row = page.locator(row_selector).first
    action = row.locator("button, input[type=submit]")
    if action.count() == 0:
        action = row.locator("a")
    assert action.count() > 0, f"no action in the first row of {row_selector}"
    return action.first


def _assert_the_page_fits(page, row_selector):
    """The page does not run sideways, the first row's action is on the screen, and the row is stacked."""
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    box = _first_row_action(page, row_selector).bounding_box()
    assert box is not None
    assert box["x"] >= 0, box
    assert box["x"] + box["width"] <= PHONE["width"] + 1, box
    # The row is a block: every cell sits on its own line.
    cells = page.locator(row_selector).first.locator("td")
    if cells.count() > 1:
        first = cells.nth(0).bounding_box()
        second = cells.nth(1).bounding_box()
        assert second["y"] >= first["y"] + first["height"] - 1, (first, second)


def _assert_every_cell_is_on_the_screen(page, row_selector):
    """The page does not run sideways, and every cell of the first row is on the screen.

    Some rows have nothing a person presses in them (an audit row, say), so the cells themselves
    are what has to fit.
    """
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    cells = page.locator(row_selector).first.locator("td")
    assert cells.count() > 0, row_selector
    for index in range(cells.count()):
        box = cells.nth(index).bounding_box()
        assert box is not None, index
        assert box["x"] >= 0, box
        assert box["x"] + box["width"] <= PHONE["width"] + 1, box
    # The row is a block: every cell sits on its own line.
    first = cells.nth(0).bounding_box()
    second = cells.nth(1).bounding_box()
    assert second["y"] >= first["y"] + first["height"] - 1, (first, second)


def test_the_needs_you_page_fits_a_phone(phone, instance):
    phone.goto(instance + "/")
    phone.wait_for_load_state("load")
    phone.wait_for_selector("#overview-waiting tbody tr")
    _assert_the_page_fits(phone, "#overview-waiting tbody tr")


def test_the_activity_page_fits_a_phone(phone, instance):
    phone.goto(instance + "/activity")
    phone.wait_for_load_state("load")
    phone.wait_for_selector("div.card:has(h2:text-is('Waiting on you')) tbody tr")
    _assert_the_page_fits(phone, "div.card:has(h2:text-is('Waiting on you')) tbody tr")


def test_the_approvals_page_fits_a_phone(phone, instance):
    phone.goto(instance + "/approvals")
    phone.wait_for_load_state("load")
    phone.wait_for_selector("div.card:has(h2:text-is('Waiting for a decision')) tbody tr")
    _assert_the_page_fits(phone, "div.card:has(h2:text-is('Waiting for a decision')) tbody tr")


def test_the_access_page_fits_a_phone(phone, instance):
    phone.goto(instance + "/access")
    phone.wait_for_load_state("load")
    phone.wait_for_selector("#access-rows tr")
    _assert_the_page_fits(phone, "#access-rows tr")


def test_the_audit_log_fits_a_phone(phone, instance):
    """The audit rows are one block per event, and every cell of the first row is on the screen."""
    phone.goto(instance + "/audit")
    phone.wait_for_load_state("load")
    phone.wait_for_selector(".audit-table tbody tr")
    _assert_every_cell_is_on_the_screen(phone, ".audit-table tbody tr")


def test_the_workspaces_page_fits_a_phone(phone, instance):
    """The workspace rows are one block per workspace, and the buttons are on the screen."""
    phone.goto(instance + "/workspaces")
    phone.wait_for_load_state("load")
    phone.wait_for_selector("table.stack-phone tbody tr")
    _assert_the_page_fits(phone, "table.stack-phone tbody tr")


def test_the_labels_in_front_of_the_values_stay_on_one_line(phone, instance):
    """The label has room of its own, so it does not wrap and crowd the value it names."""
    for route, row in (
        ("/access", "#access-rows tr.access-group-row"),
        ("/approvals", "div.card:has(h2:text-is('Waiting for a decision')) tbody tr"),
        ("/activity", "div.card:has(h2:text-is('Waiting on you')) tbody tr"),
        ("/", "#overview-waiting tbody tr"),
    ):
        phone.goto(instance + route)
        phone.wait_for_load_state("load")
        phone.wait_for_selector(row, state="attached")
        if route == "/access":  # the rows sit in closed groups: open the first one to measure a label
            phone.locator("#access-rows .access-group-toggle").first.click()
        height = phone.locator(row).first.locator("td[data-label]").first.evaluate(
            "el => getComputedStyle(el, '::before').height"
        )
        # One line of a 0.75rem label is under 20px; two lines are over it.
        assert float(height.replace("px", "")) <= 20, (route, height)


def test_change_owner_keeps_save_and_cancel_clear_of_the_close_bar(phone, instance):
    """At 390 px, with a different owner picked and the preview open, Save and Cancel are above the Close bar,
    and the Close bar sits at the very bottom of the sheet."""
    phone.goto(instance + "/credentials")
    phone.wait_for_load_state("load")
    phone.locator("[data-credential-access]").first.click()
    phone.wait_for_selector("#access-modal .modal", state="visible")
    phone.locator("#access-owner-change").click()
    options = phone.evaluate("Array.from(document.querySelectorAll('#access-owner-pick option')).map(o => o.value)")
    current = phone.evaluate("document.querySelector('#access-owner-pick').value")
    other = next(o for o in options if o and o != current)
    phone.select_option("#access-owner-pick", other)
    phone.wait_for_function("document.querySelector('#access-owner-preview').textContent.includes('Gains access')")
    phone.wait_for_timeout(300)
    geometry = phone.evaluate(
        """() => {
          const box = s => document.querySelector(s).getBoundingClientRect();
          return {save: box('#access-owner-save'), cancel: box('#access-owner-cancel'),
                  footer: box('#access-modal .modal-footer'), sheet: box('#access-modal .modal')};
        }"""
    )
    assert geometry["save"]["bottom"] <= geometry["footer"]["top"] + 1, geometry
    assert geometry["cancel"]["bottom"] <= geometry["footer"]["top"] + 1, geometry
    phone.evaluate("document.querySelector('#access-modal .modal-scroll').scrollTop = 1e6")
    phone.wait_for_timeout(200)
    footer = phone.evaluate("document.querySelector('#access-modal .modal-footer').getBoundingClientRect().bottom")
    sheet = phone.evaluate("document.querySelector('#access-modal .modal').getBoundingClientRect().bottom")
    assert abs(footer - sheet) <= 1, (footer, sheet)  # nothing is left showing under the Close bar
