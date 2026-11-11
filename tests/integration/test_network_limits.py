"""Private-address blocking and the credential-broker allowlist: report first, enforce by choice."""

import sqlite3

import pytest

from app.config import settings
from app.security.url_validation import resolve_public_url
from app.services import auth_service, network_policy_service as policy
from tests.conftest import csrf_headers
from tests.integration.test_approvals import _browser_admin

PRIVATE = "http://192.168.1.50:8080/api"


def _set(**changes):
    policy.update(changes, changed_by="admin")


def _audit(action):
    from app.database import get_db

    with get_db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM audit_log WHERE action = ?", (action,)).fetchall()]


def test_a_fresh_install_blocks_private_addresses_and_one_with_data_keeps_allowing_them():
    from app.schema import _migrate_022_private_address_policy

    def seeded(with_user):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE users (id TEXT)")
        conn.execute("CREATE TABLE agents (id TEXT)")
        conn.execute("CREATE TABLE system_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT)")
        if with_user:
            conn.execute("INSERT INTO users VALUES ('someone')")
        _migrate_022_private_address_policy(conn)
        return conn.execute("SELECT value FROM system_settings WHERE key = 'internal_hosts_mode'").fetchone()[0]

    assert seeded(False) == "block" and seeded(True) == "allow"


def test_allow_report_and_block_behave_as_named(test_client, admin_token):
    assert resolve_public_url(PRIVATE).host == "192.168.1.50"  # allow: as before
    _set(internal_mode="report")
    assert resolve_public_url(PRIVATE).host == "192.168.1.50"  # report: goes through, and is recorded once
    resolve_public_url(PRIVATE)
    seen = _audit("internal_host_would_block")
    assert [e["resource_id"] for e in seen] == ["192.168.1.50"]
    assert policy.status()["internal"]["would_block"] == 1
    _set(internal_mode="block")
    with pytest.raises(ValueError) as refused:
        resolve_public_url(PRIVATE)
    assert str(refused.value) == (
        "Blocked: 192.168.1.50 is a private address. Allow it under Settings → Security → Allowed private addresses."
    )


def test_an_allowed_host_address_or_range_gets_through_a_block(test_client, admin_token):
    _set(internal_mode="block")
    for entry in ("192.168.1.50", "192.168.1.0/24", "media-box.lan"):
        _set(allowed_internal=[entry])
        if entry == "media-box.lan":
            with pytest.raises(ValueError):
                resolve_public_url(PRIVATE)  # a host name entry does not allow an unrelated address
        else:
            assert resolve_public_url(PRIVATE).port == 8080
    _set(allowed_internal=[])
    with pytest.raises(ValueError):
        resolve_public_url(PRIVATE)
    # A public address is never touched by the setting.
    assert policy.host_allowed("example.com") is False


def test_the_environment_still_wins_and_the_dashboard_says_so(test_client, admin_token, monkeypatch):
    monkeypatch.setattr(settings, "INTERNAL_HOSTS_MODE", "block")
    assert policy.internal_hosts_mode() == "block" and policy.status()["internal"]["mode_from_environment"] is True
    with pytest.raises(ValueError, match="environment variable"):
        _set(internal_mode="allow")
    monkeypatch.setattr(settings, "INTERNAL_HOSTS_MODE", "")
    monkeypatch.setattr(settings, "BLOCK_INTERNAL_HOSTS", True)  # the older switch still means block
    assert policy.internal_hosts_mode() == "block"


def test_only_a_person_in_the_dashboard_can_change_the_limits(test_client, admin_token):
    url = "/api/network-limits"
    body = {"internal_mode": "block"}
    token, _ = auth_service.create_operator_token("admin", 15)
    assert test_client.put(url, headers={"Authorization": f"Bearer {token}"}, json=body).status_code == 403
    assert test_client.put(url, headers={"Authorization": f"Bearer {admin_token}"}, json=body).status_code == 403
    assert test_client.get(url, headers={"Authorization": f"Bearer {admin_token}"}).status_code == 200  # reading is fine
    _browser_admin(test_client)
    assert test_client.put(url, headers=csrf_headers(test_client), json={"allowed_internal": ["not a host!"]}).status_code == 400
    ok = test_client.put(url, headers=csrf_headers(test_client), json=body)
    assert ok.status_code == 200, ok.text
    changed = _audit("network_limits_changed")
    assert len(changed) == 1 and changed[0]["actor_id"] == "admin" and '"internal_mode": "block"' in changed[0]["details_json"]


def test_adding_a_service_or_binding_at_a_blocked_address_says_what_to_do_there_and_then(test_client, admin_token):
    from app.services import connector_service

    _set(internal_mode="block")
    headers = {"Authorization": f"Bearer {admin_token}"}
    r = test_client.post("/api/connector-types/create-http", headers=headers,
                         json={"display_name": "NAS", "base_url": PRIVATE, "auth_type": "none"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "PRIVATE_ADDRESS"
    assert "Allow it under Settings → Security → Allowed private addresses" in r.json()["error"]["message"]

    connector_service.create_connector_type(connector_type_id="svc", display_name="Svc", auth_type="none",
                                            supported_actions=[{"name": "GET /x", "method": "GET"}], backend_type="generic_http")
    import json

    body = {"connector_type_id": "svc", "name": "b", "scope": "user:admin", "config_json": json.dumps({"base_url": PRIVATE})}
    refused = test_client.post("/api/connector-bindings", headers=headers, json=body)
    assert refused.status_code == 400 and refused.json()["error"]["code"] == "PRIVATE_ADDRESS"
    _set(allowed_internal=["192.168.1.50"])
    assert test_client.post("/api/connector-bindings", headers=headers, json=body).status_code == 201


def _broker(client, ip="203.0.113.9"):
    return client.post("/internal/credentials/resolve", json={"variable_name": "AC_SECRET_x", "agent_id": "a"},
                       headers={"Authorization": "Broker wrong", "X-Forwarded-For": ip})


def test_the_broker_accepts_any_caller_until_a_list_is_set_then_reports_before_it_refuses(test_client, admin_token, monkeypatch):
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", "testclient")
    assert _broker(test_client).status_code == 401  # no list: straight to the credential check
    assert _audit("broker_caller_would_refuse") == []
    _set(broker_allowed=["198.51.100.0/24"])
    assert _broker(test_client).status_code == 401  # audit mode: let through (and then refused for the wrong credential)
    assert [e["resource_id"] for e in _audit("broker_caller_would_refuse")] == ["203.0.113.9"]
    assert _broker(test_client, "198.51.100.7").status_code == 401  # on the list: no new record
    assert len(_audit("broker_caller_would_refuse")) == 1
    _set(broker_mode="enforce")
    assert _broker(test_client).status_code == 403
    assert _broker(test_client, "198.51.100.7").status_code == 401
    assert _broker(test_client, "127.0.0.1").status_code == 401  # this server itself always may
    assert [e["resource_id"] for e in _audit("broker_caller_refused")] == ["203.0.113.9"]


def test_only_failed_sign_ins_count_against_an_address(test_client, admin_token):
    codes = [_broker(test_client).status_code for _ in range(63)]
    assert codes[:60] == [401] * 60 and 429 in codes[60:]


def test_a_name_that_does_not_resolve_yet_can_still_be_added_when_nothing_is_blocked(test_client, admin_token):
    headers = {"Authorization": f"Bearer {admin_token}"}
    r = test_client.post("/api/connector-types/create-http", headers=headers,
                         json={"display_name": "Later", "base_url": "http://not-yet.invalid/api", "auth_type": "none"})
    assert r.status_code in (200, 201), r.text


def test_settings_and_overview_show_the_limits_quietly(test_client, admin_token):
    _browser_admin(test_client)
    settings_page = test_client.get("/settings").text
    assert 'id="network-limits"' in settings_page and "Allowed private addresses" in settings_page
    assert "Local credential helper callers" in settings_page and "Report only" in settings_page
    overview = test_client.get("/").text
    assert "Private addresses" in overview and "Helper callers" in overview
    _set(internal_mode="report")
    policy.note_would_block("192.168.1.99", "192.168.1.99")
    assert "report only; 1 would be blocked" in test_client.get("/").text
