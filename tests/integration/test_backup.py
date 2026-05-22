from unittest.mock import patch


def test_backup_export_no_longer_requires_otp(test_client, admin_token):
    from io import BytesIO
    from cryptography.fernet import Fernet

    backup_key = Fernet.generate_key()
    with patch(
        "app.services.backup_service.build_encrypted_backup_package",
        return_value=(BytesIO(b"encrypted-zip"), backup_key),
    ):
        r = test_client.post(
            "/api/backup/export",
            headers={"Authorization": f"Bearer {admin_token}"},
        )

    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/octet-stream")
    assert r.headers["x-agent-core-backup-key"] == backup_key.decode()


def test_backup_export_requires_admin(test_client):
    from app.services import auth_service

    user = auth_service.create_user(
        "backupuser",
        "backupuser@test.local",
        "testpassword123",
        "Backup User",
        role="user",
    )
    session = auth_service.create_session(user["id"])

    r = test_client.post(
        "/api/backup/export",
        headers={"Authorization": f"Bearer {session['session_id']}"},
    )
    assert r.status_code in (400, 403), (
        f"expected 400 or 403, got {r.status_code}: {r.json()}"
    )


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
