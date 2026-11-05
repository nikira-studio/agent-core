"""Trusted devices: a long-lived session good for the Approvals routes only."""

from tests.conftest import csrf_headers
from tests.integration.test_approvals import _browser_admin, _propose, _setup, _update_target


import pytest


@pytest.fixture
def routine_agent_update(monkeypatch):
    """Treat agent.update as routine, so the phone flow can be tested with a real
    handler; the widening rule itself is tested separately."""
    import dataclasses

    from app.operations import admin_registry

    admin_registry.get("agent.update")
    op = admin_registry._REGISTRY["agent.update"]
    monkeypatch.setitem(admin_registry._REGISTRY, "agent.update",
                        dataclasses.replace(op, needs_dashboard_signin=False))


def _trust(client):
    r = client.post("/api/approvals/trust-device", headers=csrf_headers(client))
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _phone(client, device_cookie):
    """Switch the client to a phone that holds only the trusted-device cookie."""
    client.cookies.clear()
    client.cookies.set("approvals_session", device_cookie, path="/")


def test_trusting_sets_a_strict_path_limited_cookie(test_client, admin_token):
    _browser_admin(test_client)
    r = test_client.post("/api/approvals/trust-device", headers=csrf_headers(test_client))
    assert r.status_code == 200
    cookies = [h for h in r.headers.get_list("set-cookie") if h.startswith("approvals_session=")]
    assert {"Path=/approvals", "Path=/api/approvals"} == {
        part.strip() for c in cookies for part in c.split(";") if part.strip().startswith("Path=")}
    for c in cookies:
        assert "HttpOnly" in c and "SameSite=strict" in c.replace("samesite", "SameSite").replace("Strict", "strict")


def test_trusted_device_can_decide_without_signing_in(test_client, admin_token, routine_agent_update):
    key = _setup(test_client, admin_token)
    approval_id = _propose(test_client, key, [_update_target("from the phone")]).json()["data"]["approval"]["id"]
    _browser_admin(test_client)
    device = _trust(test_client)
    cookie = next(c for c in test_client.cookies.jar if c.name == "approvals_session").value
    _phone(test_client, cookie)

    r = test_client.post(f"/api/approvals/{approval_id}/decide", json={"approve": True})
    assert r.status_code == 403  # the CSRF token is still required
    from app.services.auth_service import CSRF_HEADER, csrf_token_for_session_token
    csrf = {CSRF_HEADER: csrf_token_for_session_token(cookie)}
    r = test_client.post(f"/api/approvals/{approval_id}/decide", json={"approve": True}, headers=csrf)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["approval"]["status"] == "done"
    assert device["device_id"]


def test_trusted_device_is_refused_everywhere_else(test_client, admin_token):
    _browser_admin(test_client)
    _trust(test_client)
    cookie = next(c for c in test_client.cookies.jar if c.name == "approvals_session").value
    # Even if the phone sends it as the dashboard cookie, or to other routes.
    for name in ("session_token", "approvals_session"):
        test_client.cookies.clear()
        test_client.cookies.set(name, cookie, path="/")
        for path in ("/api/agents", "/api/auth/operator-tokens", "/settings", "/api/credentials/entries"):
            r = test_client.get(path, follow_redirects=False)
            assert r.status_code in (301, 302, 303, 307, 401, 403), (name, path, r.status_code)
    from app.services.auth_service import validate_session
    assert validate_session(cookie) is None
    assert validate_session(cookie, approvals=True) is not None


def test_a_trusted_device_cannot_trust_more_devices(test_client, admin_token):
    _browser_admin(test_client)
    _trust(test_client)
    cookie = next(c for c in test_client.cookies.jar if c.name == "approvals_session").value
    _phone(test_client, cookie)
    from app.services.auth_service import CSRF_HEADER, csrf_token_for_session_token
    r = test_client.post("/api/approvals/trust-device", headers={CSRF_HEADER: csrf_token_for_session_token(cookie)})
    assert r.status_code == 401


def test_revoking_a_device_ends_it(test_client, admin_token):
    _browser_admin(test_client)
    device = _trust(test_client)
    listed = test_client.get("/api/approvals/trusted-devices").json()["data"]["devices"]
    assert [d["id"] for d in listed] == [device["device_id"]]
    assert test_client.delete(f"/api/approvals/trusted-devices/{device['device_id']}",
                              headers=csrf_headers(test_client)).status_code == 200
    assert test_client.get("/api/approvals/trusted-devices").json()["data"]["devices"] == []
    from app.services.auth_service import validate_session
    cookie = next(c for c in test_client.cookies.jar if c.name == "approvals_session").value
    assert validate_session(cookie, approvals=True) is None


def test_a_trusted_device_expires(test_client, admin_token, monkeypatch):
    from datetime import timedelta

    from app.services import auth_service
    from app.time_utils import utc_now

    _browser_admin(test_client)
    _trust(test_client)
    cookie = next(c for c in test_client.cookies.jar if c.name == "approvals_session").value
    monkeypatch.setattr(auth_service, "utc_now", lambda: utc_now() + timedelta(days=31))
    assert auth_service.validate_session(cookie, approvals=True, inactivity_minutes=10**6) is None


def test_phone_flow_through_the_page(test_client, admin_token, routine_agent_update):
    """Open the approval page with only the device cookie, use the CSRF token
    the page embeds, and decide: no sign-in anywhere."""
    import re

    key = _setup(test_client, admin_token)
    approval_id = _propose(test_client, key, [_update_target("phone flow")]).json()["data"]["approval"]["id"]
    _browser_admin(test_client)
    _trust(test_client)
    cookie = next(c for c in test_client.cookies.jar if c.name == "approvals_session").value
    _phone(test_client, cookie)

    page = test_client.get(f"/approvals/{approval_id}", follow_redirects=False)
    assert page.status_code == 200, page.status_code
    token = re.search(r'var t="([0-9a-f]{64})"', page.text)
    assert token and token.group(1), "the page must embed a CSRF token for the device session"
    r = test_client.post(f"/api/approvals/{approval_id}/decide", json={"approve": True},
                         headers={"X-CSRF-Token": token.group(1)})
    assert r.status_code == 200 and r.json()["data"]["approval"]["status"] == "done"
    # The same device still cannot open other dashboard pages.
    assert test_client.get("/settings", follow_redirects=False).status_code in (302, 303, 307)


def test_widening_changes_need_a_dashboard_signin(test_client, admin_token):
    key = _setup(test_client, admin_token)
    approval_id = _propose(test_client, key, [_update_target("widen")]).json()["data"]["approval"]["id"]
    _browser_admin(test_client)
    _trust(test_client)
    cookie = next(c for c in test_client.cookies.jar if c.name == "approvals_session").value
    _phone(test_client, cookie)
    from app.services.auth_service import CSRF_HEADER, csrf_token_for_session_token
    csrf = {CSRF_HEADER: csrf_token_for_session_token(cookie)}
    r = test_client.post(f"/api/approvals/{approval_id}/decide", json={"approve": True}, headers=csrf)
    assert r.status_code == 403 and r.json()["error"]["code"] == "DASHBOARD_SIGNIN_REQUIRED"
    # Still pending, so the dashboard can approve it.
    _browser_admin(test_client)
    r = test_client.post(f"/api/approvals/{approval_id}/decide", json={"approve": True}, headers=csrf_headers(test_client))
    assert r.json()["data"]["approval"]["status"] == "done"


def test_every_operation_that_widens_an_agent_needs_a_dashboard_signin():
    from app.operations import admin_registry

    widening = {n for n in admin_registry.names() if admin_registry.get(n).needs_dashboard_signin}
    assert widening == {"agent.create", "agent.update", "credential_grant.create",
                        "binding.create", "binding.update", "connector_type.actions"}
