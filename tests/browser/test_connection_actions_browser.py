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
