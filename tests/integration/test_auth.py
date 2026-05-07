from app.database import get_db
from app.security.rate_limiter import RL


def test_health_endpoint(test_client):
    r = test_client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] == True


def test_spec_public_endpoint(test_client):
    r = test_client.get("/spec/public")
    assert r.status_code == 200
    assert r.json()["ok"] == True


def test_register_first_admin(test_client, clean_db):
    r = test_client.post("/api/auth/register", json={
        "email": "admin@test.local",
        "password": "testpassword123",
        "display_name": "Admin Test",
    })
    assert r.status_code == 200
    assert r.json()["ok"] == True
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
    assert r.json()["ok"] == True
    assert "session_id" in r.json()["data"]


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


def test_protected_endpoint_without_auth(test_client):
    r = test_client.get("/api/agents")
    assert r.status_code == 401


def test_api_error_envelope(test_client):
    r = test_client.get("/api/agents")
    assert r.status_code == 401
    assert r.json()["ok"] == False
    assert "error" in r.json()
    assert "code" in r.json()["error"]
