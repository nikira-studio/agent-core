"""A generic web service cannot be saved without an address, and the form asks for one."""

from app.services import connector_service


def _type(endpoint_url=None, type_id="gen-http"):
    connector_service.create_connector_type(
        connector_type_id=type_id,
        display_name="Generic web",
        auth_type="none",
        supported_actions=[{"name": "GET /x", "method": "GET"}],
        backend_type="generic_http",
        endpoint_url=endpoint_url,
    )


def _create(client, admin_token, config_json, type_id="gen-http"):
    client.post("/api/workspaces", headers={"Authorization": f"Bearer {admin_token}"}, json={"id": "gw", "name": "GW"})
    return client.post(
        "/api/connector-bindings",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"connector_type_id": type_id, "name": "B", "scope": "workspace:gw", "config_json": config_json},
    )


def test_saving_without_a_base_url_says_so(test_client, admin_token):
    _type()
    for config in (None, "{}", '{"default_params": {}}'):
        r = _create(test_client, admin_token, config)
        assert r.status_code == 400 and r.json()["error"]["code"] == "BASE_URL_REQUIRED", r.text
        assert "Base URL" in r.json()["error"]["message"]


def test_a_base_url_in_the_settings_is_enough(test_client, admin_token):
    _type()
    r = _create(test_client, admin_token, '{"base_url": "https://api.example.com"}')
    assert r.status_code == 201, r.text


def test_a_service_with_its_own_address_needs_none(test_client, admin_token):
    _type(endpoint_url="https://api.example.com", type_id="gen-http-ready")
    assert _create(test_client, admin_token, None, "gen-http-ready").status_code == 201


def test_editing_the_settings_cannot_remove_the_address(test_client, admin_token):
    _type()
    binding = _create(test_client, admin_token, '{"base_url": "https://api.example.com"}').json()["data"]["binding"]
    r = test_client.put(
        f"/api/connector-bindings/{binding['id']}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"config_json": "{}"},
    )
    assert r.status_code == 400 and r.json()["error"]["code"] == "BASE_URL_REQUIRED"


def test_the_create_form_asks_for_a_base_url_only_for_a_generic_service(test_client, admin_token):
    _type()
    html = test_client.get("/connectors", headers={"Authorization": f"Bearer {admin_token}"}).text
    assert 'id="binding-base-url"' in html
    assert 'data-needs-base-url="1"' in html
