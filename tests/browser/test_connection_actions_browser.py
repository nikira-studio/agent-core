"""Edit connection > "What this connection can do", in a real browser."""

import json

import pytest

from tests.browser.conftest import seed

SEED = '''
import json
from app.database import get_db
from app.services import connector_service as cs
with get_db() as conn:
    owner = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
actions = [
    {"name": "list_things", "side_effect": "read", "description": "List things", "input_schema": {"type": "object"}},
    {"name": "create_thing", "side_effect": "write", "description": "Make a thing", "input_schema": {"type": "object"}},
    {"name": "delete_thing", "side_effect": "destructive", "description": "Remove a thing", "input_schema": {"type": "object"}},
]
requests = {a["name"]: {"method": "GET" if a["side_effect"] == "read" else "POST", "path": "/" + a["name"]} for a in actions}
cs.create_connector_type("pick-svc", "Pick service", auth_type="none", provider_type="builtin", supported_actions=actions,
                         backend_type="http", backend_json=json.dumps({"type": "http", "requests": requests}),
                         disabled_actions=["delete_thing"])
cs.create_binding("pick-svc", "Pick connection", f"user:{owner}", config_json='{"base_url": "http://example.test"}')
'''


@pytest.fixture(scope="module")
def connections(instance, admin_page):
    seed(instance, SEED)
    admin_page.goto(instance + "/connectors")
    admin_page.wait_for_load_state("load")
    return admin_page


def _binding_id(page):
    return page.locator("tr[data-binding-id]").first.get_attribute("data-binding-id")


def _open_edit(page):
    page.locator("tr[data-binding-id]", has_text="Pick connection").locator("[data-binding-edit]").click()
    page.wait_for_selector("#edit-binding-modal .modal", state="visible")


def _api(page, path):
    return page.evaluate("async (p) => (await apiFetch(p)).data", path)


def test_the_default_is_everything_the_service_allows(connections):
    _open_edit(connections)
    assert connections.locator("#edit-binding-actions-group input[value=all]").is_checked()
    assert not connections.locator("#edit-binding-actions-list").is_visible()
    connections.keyboard.press("Escape")


def test_choose_actions_lists_them_greys_what_the_service_switched_off_and_saves(connections):
    _open_edit(connections)
    group = connections.locator("#edit-binding-actions-group")
    group.get_by_label("Choose actions").check()
    connections.wait_for_selector("#edit-binding-actions-list input", state="visible")
    boxes = connections.locator("#edit-binding-actions-list input[type=checkbox]")
    assert boxes.count() == 3
    off_for_everyone = connections.locator("#edit-binding-actions-list input[data-action=delete_thing]")
    assert off_for_everyone.is_disabled() and not off_for_everyone.is_checked()  # a connection cannot switch it on
    assert "switched off for everyone" in connections.locator("#edit-binding-actions-list").inner_text()
    connections.locator("#edit-binding-actions-list input[data-action=create_thing]").uncheck()
    connections.locator("#edit-binding-form button[type=submit]").click()
    connections.wait_for_timeout(800)
    stored = _api(connections, f"/api/connector-bindings/{_binding_id(connections)}")["binding"]
    assert stored["actions_mode"] == "custom" and stored["disabled_actions"] == ["create_thing"]
    assert "Chosen actions" in connections.locator("tr[data-binding-id]", has_text="Pick connection").inner_text()


def test_the_read_only_preset_saves_and_the_row_says_so(connections):
    connections.goto(connections.url)
    connections.wait_for_load_state("load")
    _open_edit(connections)
    connections.locator("#edit-binding-actions-group").get_by_label("Read-only").check()
    connections.locator("#edit-binding-form button[type=submit]").click()
    connections.wait_for_timeout(800)
    stored = _api(connections, f"/api/connector-bindings/{_binding_id(connections)}")["binding"]
    assert stored["actions_mode"] == "read_only"
    row = connections.locator("tr[data-binding-id]", has_text="Pick connection").inner_text()
    assert "Read-only" in row and "1 of 3 actions" in row
    menu = _api(connections, f"/api/connector-bindings/{_binding_id(connections)}/tools")
    assert [t["action"] for t in menu["tools"]] == ["list_things"]


def test_saving_without_changing_the_choice_never_rewrites_it(connections):
    """The dialog sends nothing unless the person changed the choice, so opening and saving cannot widen a connection."""
    connections.goto(connections.url)
    connections.wait_for_load_state("load")
    bid = _binding_id(connections)
    connections.evaluate("async (id) => apiFetch('/api/connector-bindings/' + id, {method: 'PUT', body: JSON.stringify({actions_mode: 'custom', disabled_actions: ['create_thing']})})", bid)
    connections.reload()
    connections.wait_for_load_state("load")
    _open_edit(connections)
    connections.wait_for_selector("#edit-binding-actions-list input", state="visible")
    # simulate the list failing to load: nothing about the actions may be sent
    connections.evaluate("document.getElementById('edit-binding-actions-group').dataset.loaded = ''")
    sent = []
    connections.on("request", lambda r: sent.append(r.post_data) if r.method == "PUT" else None)
    connections.locator("#edit-binding-form button[type=submit]").click()
    connections.wait_for_timeout(800)
    assert sent and "actions_mode" not in sent[0] and "disabled_actions" not in sent[0]
    stored = _api(connections, f"/api/connector-bindings/{bid}")["binding"]
    assert stored["actions_mode"] == "custom" and stored["disabled_actions"] == ["create_thing"]


def test_a_connection_that_allows_nothing_yet_shows_every_action_unticked(connections):
    connections.goto(connections.url)
    connections.wait_for_load_state("load")
    bid = _binding_id(connections)
    connections.evaluate("async (id) => apiFetch('/api/connector-bindings/' + id, {method: 'PUT', body: JSON.stringify({actions_mode: 'none'})})", bid)
    connections.reload()
    connections.wait_for_load_state("load")
    assert "Nothing allowed yet" in connections.locator("tr[data-binding-id]", has_text="Pick connection").inner_text()
    _open_edit(connections)
    connections.wait_for_selector("#edit-binding-actions-list input", state="visible")
    enabled = connections.locator("#edit-binding-actions-list input[type=checkbox]:not(:disabled)")
    assert enabled.count() == 2 and all(not enabled.nth(i).is_checked() for i in range(2))
    connections.keyboard.press("Escape")


def test_the_dialog_says_whether_the_limit_holds_whenever_the_choice_is_not_everything(connections):
    connections.goto(connections.url)
    connections.wait_for_load_state("load")
    bid = _binding_id(connections)
    connections.evaluate("async (id) => apiFetch('/api/connector-bindings/' + id, {method: 'PUT', body: JSON.stringify({actions_mode: 'all'})})", bid)
    connections.reload()
    connections.wait_for_load_state("load")
    _open_edit(connections)
    box = connections.locator("#edit-binding-credential-reach")
    connections.wait_for_timeout(600)
    assert not box.is_visible()  # "everything": nothing to say
    connections.locator("#edit-binding-actions-group").get_by_label("Read-only").check()
    connections.wait_for_selector("#edit-binding-credential-reach", state="visible")
    assert "no credential" in box.inner_text() and "this limit holds" in box.inner_text()
    connections.locator("#edit-binding-actions-group").get_by_label("Everything the service allows").check()
    assert not box.is_visible()
    connections.keyboard.press("Escape")
