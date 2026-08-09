import json

import pytest

from app.database import get_db
from app.security.rate_limiter import RL


def test_health_endpoint(test_client):
    r = test_client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_spec_public_endpoint(test_client):
    r = test_client.get("/spec/public")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_register_first_admin(test_client, clean_db):
    r = test_client.post("/api/auth/register", json={
        "email": "admin@test.local",
        "password": "testpassword123",
        "display_name": "Admin Test",
    })
    assert r.status_code == 200
    assert r.json()["ok"] is True
    data = r.json()["data"]
    assert data["role"] == "admin"


def test_register_records_user_registered_audit_event(test_client, clean_db, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "TRUSTED_PROXIES", "testclient")
    r = test_client.post(
        "/api/auth/register",
        headers={"X-Forwarded-For": "203.0.113.9"},
        json={
            "email": "audit@test.local",
            "password": "testpassword123",
            "display_name": "Audit Test",
        },
    )
    assert r.status_code == 200

    with get_db() as conn:
        row = conn.execute(
            "SELECT action, ip_address FROM audit_log WHERE actor_id = ? ORDER BY id DESC LIMIT 1",
            ("audit",),
        ).fetchone()

    assert row["action"] == "user_registered"
    assert row["ip_address"] == "203.0.113.9"


def test_login_invalid_credentials(test_client, clean_db):
    r = test_client.post("/api/auth/login", json={
        "email": "nobody@test.local",
        "password": "wrong",
    })
    assert r.status_code == 401


def test_login_valid_credentials(test_client, clean_db):
    test_client.post("/api/auth/register", json={
        "email": "user@test.local",
        "password": "testpassword123",
        "display_name": "Test User",
    })
    r = test_client.post("/api/auth/login", json={
        "email": "user@test.local",
        "password": "testpassword123",
    })
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert "session_id" in r.json()["data"]

    with get_db() as conn:
        row = conn.execute(
            "SELECT details_json FROM audit_log WHERE action = 'session_login' AND actor_id = ? ORDER BY id DESC LIMIT 1",
            ("user",),
        ).fetchone()

    assert row is not None
    details = json.loads(row["details_json"])
    assert details["user_id"] == "user"
    assert details["role"] == "admin"
    assert details["otp_required"] is False


def test_disabling_a_user_revokes_sessions_and_blocks_login(test_client, admin_token):
    from app.services.auth_service import create_session, create_user

    create_user("disabled", "disabled@test.local", "testpassword123", "Disabled User")
    old_session = create_session("disabled")["session_id"]
    disabled = test_client.put(
        "/api/auth/users/disabled",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"is_active": False},
    )
    assert disabled.status_code == 200, disabled.json()

    existing = test_client.get(
        "/api/agents", headers={"Authorization": f"Bearer {old_session}"}
    )
    assert existing.status_code == 401, existing.json()
    login = test_client.post(
        "/api/auth/login",
        json={"email": "disabled@test.local", "password": "testpassword123"},
    )
    assert login.status_code == 401
    assert login.json()["error"]["code"] == "INVALID_CREDENTIALS"
    with pytest.raises(ValueError, match="inactive"):
        create_session("disabled")


def test_effective_authority_reports_current_permanent_identity(test_client, agent_token):
    response = test_client.get(
        "/api/auth/effective-authority",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert response.status_code == 200, response.json()
    authority = response.json()["data"]["authority"]
    assert authority["authenticated_actor"] == {"type": "agent", "id": "testagent"}
    assert authority["executor_agent_id"] == "testagent"
    assert authority["authorization_mode"] == "permanent"
    assert authority["grant_id"] is None


def test_login_failed_attempts_are_rate_limited(test_client, clean_db):
    RL._buckets.pop("login_failed:user:user", None)
    test_client.post("/api/auth/register", json={
        "email": "user@test.local",
        "password": "testpassword123",
        "display_name": "Test User",
    })

    for _ in range(10):
        r = test_client.post("/api/auth/login", json={
            "email": "user@test.local",
            "password": "wrong-password",
        })
        assert r.status_code == 401

    limited = test_client.post("/api/auth/login", json={
        "email": "user@test.local",
        "password": "wrong-password",
    })
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "RATE_LIMITED"


def test_login_unknown_user_attempts_are_rate_limited_by_ip(test_client, clean_db):
    RL._buckets.pop("login_failed:user:testclient", None)

    for _ in range(10):
        r = test_client.post(
            "/api/auth/login",
            json={"email": "missing@test.local", "password": "wrong"},
        )
        assert r.status_code == 401

    limited = test_client.post(
        "/api/auth/login",
        json={"email": "missing@test.local", "password": "wrong"},
    )
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "RATE_LIMITED"


def test_protected_endpoint_without_auth(test_client):
    r = test_client.get("/api/agents")
    assert r.status_code == 401


def test_api_error_envelope(test_client):
    r = test_client.get("/api/agents")
    assert r.status_code == 401
    assert r.json()["ok"] is False
    assert "error" in r.json()
    assert "code" in r.json()["error"]
