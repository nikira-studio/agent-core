"""Hardening item 11: login sessions are Bearer keys only on /api/auth/*,
operator tokens act as a person elsewhere, and cookie writes need CSRF."""

import secrets

from tests.conftest import csrf_headers

_PW = secrets.token_hex(12)  # a throwaway login for these tests


def _login(client, email="op@test.local"):
    client.post("/api/auth/register", json={"email": email, "password": _PW, "display_name": "Op"})
    r = client.post("/api/auth/login", json={"email": email, "password": _PW})
    assert r.status_code == 200, r.text
    return r.json()["data"]


def test_login_session_is_a_bearer_key_only_on_auth_routes(test_client):
    session = _login(test_client)["session_id"]
    test_client.cookies.clear()
    bearer = {"Authorization": f"Bearer {session}"}

    assert test_client.get("/api/auth/effective-authority", headers=bearer).status_code == 200
    r = test_client.get("/api/agents", headers=bearer)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "SESSION_BEARER_NOT_ALLOWED"
    assert test_client.post("/mcp", headers=bearer, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 401


def test_operator_token_lifecycle(test_client):
    data = _login(test_client)
    r = test_client.post("/api/auth/operator-tokens", json={"minutes": 15, "label": "install script"},
                         headers=csrf_headers(test_client))
    assert r.status_code == 200, r.text
    token, token_id = r.json()["data"]["token"], r.json()["data"]["id"]
    assert token.startswith("ac_op_")

    listed = test_client.get("/api/auth/operator-tokens").json()["data"]["tokens"]
    assert [t["id"] for t in listed] == [token_id] and "token" not in listed[0]

    jar = dict(test_client.cookies)
    test_client.cookies.clear()
    bearer = {"Authorization": f"Bearer {token}"}
    who = test_client.get("/api/auth/effective-authority", headers=bearer).json()["data"]["authority"]
    assert who["authenticated_actor"]["id"] == data["user_id"]
    assert test_client.get("/api/agents", headers=bearer).status_code == 200

    test_client.cookies.update(jar)
    assert test_client.delete(f"/api/auth/operator-tokens/{token_id}", headers=csrf_headers(test_client)).status_code == 200
    test_client.cookies.clear()
    assert test_client.get("/api/agents", headers=bearer).status_code == 401


def test_operator_tokens_need_the_browser_session(test_client):
    session = _login(test_client)["session_id"]
    test_client.cookies.clear()
    r = test_client.post("/api/auth/operator-tokens", json={"minutes": 15},
                         headers={"Authorization": f"Bearer {session}"})
    assert r.status_code == 403  # an app holding a Bearer login session cannot widen it


def test_operator_token_cannot_mint_another(test_client):
    _login(test_client)
    token = test_client.post("/api/auth/operator-tokens", json={"minutes": 15},
                             headers=csrf_headers(test_client)).json()["data"]["token"]
    test_client.cookies.clear()
    r = test_client.post("/api/auth/operator-tokens", json={"minutes": 15},
                         headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


def test_operator_token_minutes_are_bounded(test_client):
    _login(test_client)
    for minutes in (1, 61):
        r = test_client.post("/api/auth/operator-tokens", json={"minutes": minutes}, headers=csrf_headers(test_client))
        assert r.status_code == 400


def test_expired_operator_token_is_refused(test_client, monkeypatch):
    from datetime import timedelta

    from app.services import auth_service
    from app.time_utils import utc_now

    data = _login(test_client)
    token, _ = auth_service.create_operator_token(data["user_id"], 5)
    later = utc_now() + timedelta(minutes=6)
    monkeypatch.setattr(auth_service, "utc_now", lambda: later)
    assert auth_service.validate_operator_token(token) is None


def test_cookie_write_needs_the_csrf_header(test_client):
    _login(test_client)
    body = {"timezone": "UTC"}
    r = test_client.post("/api/dashboard/user-settings", json=body)
    assert r.status_code == 403 and r.json()["error"]["code"] == "CSRF_FAILED"
    r = test_client.post("/api/dashboard/user-settings", json=body, headers={"X-CSRF-Token": "0" * 64})
    assert r.status_code == 403
    assert test_client.post("/api/dashboard/user-settings", json=body, headers=csrf_headers(test_client)).status_code == 200
    # Reads and signing out need no token.
    assert test_client.get("/api/auth/operator-tokens").status_code == 200
    assert test_client.post("/api/auth/logout").status_code == 200


def test_dashboard_pages_carry_the_csrf_token(test_client):
    _login(test_client)
    html = test_client.get("/settings").text
    assert csrf_headers(test_client)["X-CSRF-Token"] in html
    assert "window.fetch=function" in html
