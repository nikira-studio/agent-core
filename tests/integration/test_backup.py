from unittest.mock import patch

from app.branding import BACKUP_KEY_HEADER
from tests.conftest import operator_token_for


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
    # The key is in the response body, never in a header; the file is a second request.
    assert BACKUP_KEY_HEADER.lower() not in r.headers
    data = r.json()["data"]
    assert data["backup_key"] == backup_key.decode() and data["download_path"].startswith("/api/backup/download/")
    first = test_client.get(data["download_path"], headers={"Authorization": f"Bearer {admin_token}"})
    assert first.status_code == 200 and first.content == b"encrypted-zip"
    assert first.headers["content-type"].startswith("application/octet-stream")
    assert BACKUP_KEY_HEADER.lower() not in first.headers
    # It can be taken once.
    again = test_client.get(data["download_path"], headers={"Authorization": f"Bearer {admin_token}"})
    assert again.status_code == 404


def test_a_backup_download_is_for_the_admin_who_made_it_and_expires(test_client, admin_token):
    import json
    import time
    from io import BytesIO

    from app.routes import backup as backup_routes
    from app.services import auth_service

    with patch(
        "app.services.backup_service.build_encrypted_backup_package",
        return_value=(BytesIO(b"encrypted-zip"), b"k" * 44),
    ):
        made = test_client.post("/api/backup/export", headers={"Authorization": f"Bearer {admin_token}"}).json()["data"]
    other = auth_service.create_user("admin2", "admin2@test.local", "testpassword123", "Admin Two", role="admin")
    stolen = test_client.get(made["download_path"], headers={"Authorization": f"Bearer {operator_token_for(other['id'])}"})
    assert stolen.status_code == 404  # someone else's download is not even acknowledged
    assert test_client.get("/api/backup/download/not-a-real-id", headers={"Authorization": f"Bearer {admin_token}"}).status_code == 404
    download_id = made["download_path"].rsplit("/", 1)[1]
    meta_path = backup_routes._downloads_dir() / f"{download_id}.json"
    meta = json.loads(meta_path.read_text())
    meta["expires_at"] = time.time() - 1
    meta_path.write_text(json.dumps(meta))
    expired = test_client.get(made["download_path"], headers={"Authorization": f"Bearer {admin_token}"})
    assert expired.status_code == 404
    assert not (backup_routes._downloads_dir() / f"{download_id}.enc").exists()  # swept


def test_backup_export_requires_admin(test_client):
    from app.services import auth_service

    user = auth_service.create_user(
        "backupuser",
        "backupuser@test.local",
        "testpassword123",
        "Backup User",
        role="user",
    )
    r = test_client.post(
        "/api/backup/export",
        headers={"Authorization": f"Bearer {operator_token_for(user['id'])}"},
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


def test_maintenance_status_requires_admin(test_client, agent_token):
    r = test_client.get(
        "/api/backup/maintenance/status",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert r.status_code in (401, 403)


def test_maintenance_status_reflects_manual_run(test_client, admin_token):
    r = test_client.get(
        "/api/backup/maintenance/status",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["last_run_at"] is None

    r = test_client.post(
        "/api/backup/maintenance",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200, r.text

    r = test_client.get(
        "/api/backup/maintenance/status",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    data = r.json()["data"]
    assert data["last_run_at"] is not None
    assert data["last_run_by"] == "manual"
    assert data["last_run_summary"]["stale_activities_marked"] == 0
    assert "interval_minutes" in data
    assert "scheduler_enabled" in data


def test_backup_startup_checks(test_client, admin_token):
    r = test_client.get(
        "/api/backup/startup-checks",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert "checks" in data
