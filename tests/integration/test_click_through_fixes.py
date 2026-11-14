"""Fixes from a click-through of the dashboard: what a deleted credential leaves behind, plain words, labelled buttons."""

import json
import re

import pytest

from app.database import get_db
from app.services import connector_service, credential_service
from app.services.webhook_service import WEBHOOK_EVENT_LABELS, WEBHOOK_EVENT_TYPES


@pytest.fixture
def authenticated_client(test_client, clean_db):
    from app.services.auth_service import create_session, create_user

    create_user("admin", "admin@test.local", "testpassword123", "Admin", "admin")
    test_client.cookies.set("session_token", create_session("admin", channel="dashboard")["session_id"])
    return test_client


def _service_with_credential():
    connector_service.create_connector_type(
        connector_type_id="billing-api",
        display_name="Billing API",
        auth_type="bearer",
        supported_actions=[{"name": "list", "side_effect": "read"}],
        provider_type="openapi",
        backend_type="generic_http",
    )
    cred = credential_service.create_credential("workspace:ct-a", "billing-token", "secret-abc")
    binding = connector_service.create_binding(
        "billing-api", "Billing prod", "workspace:ct-a", credential_id=cred["id"],
        config_json=json.dumps({"base_url": "http://127.0.0.1:9"}),
    )
    return cred, binding


def test_deleting_a_credential_marks_the_connections_that_used_it(test_client, admin_token):
    cred, binding = _service_with_credential()
    assert credential_service.delete_credential(cred["id"])
    row = connector_service.get_binding(binding["id"])
    assert row["last_tested_at"] is None
    assert "credential was deleted" in row["last_error"]


def test_a_connection_whose_credential_is_gone_fails_its_test_plainly(test_client, admin_token):
    cred, binding = _service_with_credential()
    credential_service.delete_credential(cred["id"])
    result = connector_service.test_binding(binding["id"])
    assert result["success"] is False and result["error_code"] == "NO_CREDENTIAL"
    assert "attach one" in result["error"]


def test_every_webhook_event_has_a_plain_label_and_sentence():
    assert set(WEBHOOK_EVENT_LABELS) == set(WEBHOOK_EVENT_TYPES)
    for event, (label, sentence) in WEBHOOK_EVENT_LABELS.items():
        assert label and sentence.endswith(".")
        assert "_" not in label and "_" not in sentence


@pytest.mark.parametrize("path", ["/", "/connectors", "/credentials", "/webhooks", "/settings", "/users", "/audit"])
def test_pages_do_not_show_internal_vocabulary(authenticated_client, path):
    html = authenticated_client.get(path).text
    html = re.sub(r"<script.*?</script>", "", html, flags=re.S)
    html = re.sub(r"<style.*?</style>", "", html, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", html)
    for word in ("binding", "Binding", "connector type", "ALLOWED_INTERNAL_HOSTS", "Overview"):
        assert word not in text, (path, word)


def test_every_delete_button_says_delete():
    import pathlib

    for file in pathlib.Path("app/routes").glob("*.py"):
        source = file.read_text()
        for match in re.finditer(r"<button[^>]*>\s*\{?(?:get_icon\('delete'[^)]*\)\}?)\s*(?P<rest>[^<]*)</button>", source):
            assert "Delete" in match.group("rest"), (file.name, match.group(0)[:120])


def test_two_step_wording_is_plain(authenticated_client):
    assert "Two-Factor" not in authenticated_client.get("/settings/otp").text
    login = authenticated_client.post("/api/auth/otp/verify", json={"otp_code": "000000"})
    assert "OTP" not in login.text


def test_a_restore_tells_the_person_what_happened():
    from app.routes import settings_page
    import inspect

    source = inspect.getsource(settings_page)
    assert "Restore complete." in source and "anything added after it is gone" in source


def _service_card_chip(client, monkeypatch, entries):
    from app.services import adapter_loader

    for type_id, name in (("local-thing", "Local thing"), ("firecrawl", "Firecrawl")):
        connector_service.create_connector_type(
            connector_type_id=type_id, display_name=name, auth_type="bearer", supported_actions=[],
            provider_type="builtin", backend_type="http",
        )
    monkeypatch.setattr(adapter_loader, "list_available_adapters", lambda: entries)
    html = client.get("/connectors").text
    return {
        name: re.search(rf"{name}</div>.*?(Built-in|Added here)</span>", html, re.S).group(1)
        for name in ("Local thing", "Firecrawl")
    }


def test_a_service_an_installer_added_is_not_called_built_in(authenticated_client, monkeypatch):
    chips = _service_card_chip(
        authenticated_client, monkeypatch,
        [{"id": "local-thing", "shipped": False, "installed": True}, {"id": "firecrawl", "shipped": True, "installed": True}],
    )
    assert chips == {"Local thing": "Added here", "Firecrawl": "Built-in"}


def test_the_adapter_library_says_which_adapters_ship_with_the_app(test_client, admin_token):
    from app.services import adapter_loader

    shipped = {entry["id"]: entry["shipped"] for entry in adapter_loader.list_available_adapters()}
    assert shipped.get("firecrawl") is True


def test_row_menus_float_over_the_page_and_service_card_buttons_wrap():
    import pathlib

    css = pathlib.Path("app/dashboard/static/css/dashboard.css").read_text()
    menu = css[css.index(".row-more-list {"):]
    assert "position: fixed" in menu.split("}")[0]
    card = css[css.index(".connector-card-actions {"):].split("}")[0]
    assert "flex-wrap: wrap" in card and "overflow" not in card
    js = pathlib.Path("app/dashboard/static/js/dashboard.js").read_text()
    assert "details.row-more[open]" in js and "'Escape'" in js


def test_every_service_card_has_one_origin_chip_and_no_installed_or_transport_chip(authenticated_client):
    import re

    from app.services import connector_service

    connector_service.create_connector_type("mcp-added", "Added MCP", provider_type="mcp", auth_type="none",
                                            endpoint_url="https://mcp.example.com/mcp", transport_type="streamable_http",
                                            tool_snapshot_json='{"tools": []}', supported_actions=["a", "b"])
    connector_service.create_connector_type("spec-added", "Added spec", provider_type="openapi", auth_type="none",
                                            supported_actions=["c"])
    html = authenticated_client.get("/connectors").text
    cards = html.split("<div class='connector-type-card'>")[1:]
    assert cards
    for card in cards:
        card = card.split("connector-card-actions", 1)[0]
        assert len(re.findall(r'class="origin-chip"', card)) == 1, card[:200]
        assert ">Installed<" not in card and ">MCP<" not in card and ">HTTP<" not in card and ">API<" not in card
    added = next(c for c in cards if "Added MCP" in c)
    assert "Added here" in added and "MCP server" in added.split("connector-type-desc", 1)[0]  # how it talks is on the meta line
    assert "API spec" in next(c for c in cards if "Added spec" in c)
    assert "Built-in" in next(c for c in cards if "Generic HTTP API" in c)  # the seeded one
    assert "Added here" in next(c for c in cards if "Added spec" in c)
