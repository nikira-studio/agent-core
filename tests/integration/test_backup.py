import pytest
from io import BytesIO
from unittest.mock import patch


def test_backup_export_requires_otp(test_client, admin_token):
    r = test_client.post(
        "/api/backup/export",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"otp_code": "000000"},
    )
    assert r.status_code in (400, 401)


def test_backup_export_accepts_backup_code(test_client, admin_token):
    with patch("app.routes.backup.is_otp_enrolled", return_value=True), \
         patch("app.routes.backup.verify_otp_or_backup_code", return_value=True), \
         patch("app.services.backup_service.build_backup_zip", return_value=BytesIO(b"zip")):
        r = test_client.post(
            "/api/backup/export",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"otp_code": "a" * 32},
        )

    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/zip")


def test_backup_export_requires_admin(test_client):
    r = test_client.post("/api/auth/register", json={
        "email": "backupuser@test.local",
        "password": "testpassword123",
        "display_name": "Backup User",
    })
    assert r.status_code == 200
    r = test_client.post("/api/auth/login", json={
        "email": "backupuser@test.local",
        "password": "testpassword123",
    })
    assert r.status_code == 200
    user_token = r.json()["data"]["session_id"]

    r = test_client.post(
        "/api/backup/export",
        headers={"Authorization": f"Bearer {user_token}"},
        json={"otp_code": "000000"},
    )
    assert r.status_code in (400, 403), f"expected 400 or 403, got {r.status_code}: {r.json()}"


def test_backup_maintenance_requires_admin(test_client, agent_token):
    r = test_client.post(
        "/api/backup/maintenance",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={},
    )
    assert r.status_code in (401, 403)


def test_backup_startup_checks(test_client, admin_token):
    r = test_client.get(
        "/api/backup/startup-checks",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert "checks" in data
