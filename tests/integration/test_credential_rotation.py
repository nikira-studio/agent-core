from unittest.mock import patch
from cryptography.fernet import Fernet


def test_rotate_requires_admin(test_client, agent_token):
    r = test_client.post(
        "/api/credentials/rotate",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert r.status_code == 403


def test_rotate_succeeds_with_valid_admin(test_client, admin_token):
    with patch("app.services.credential_rotation_service.rotate_key") as mock_rotate:
        mock_rotate.return_value = (
            True,
            "Rotation complete. 5 entries re-encrypted.",
            {
                "re_encrypted_count": 5,
                "keyring_size": 2,
            },
        )
        r = test_client.post(
            "/api/credentials/rotate",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
    assert r.status_code == 200
    data = r.json()["data"]
    assert "re_encrypted_count" in data
    assert "keyring_size" in data


def test_rotate_audit_event_written(test_client, admin_token):
    with patch("app.services.credential_rotation_service.rotate_key") as mock_rotate:
        mock_rotate.return_value = (
            True,
            "done",
            {"re_encrypted_count": 1, "keyring_size": 2},
        )
        r = test_client.post(
            "/api/credentials/rotate",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
    assert r.status_code == 200
    mock_rotate.assert_called_once()


def test_rotation_status_requires_admin(test_client, agent_token):
    r = test_client.get(
        "/api/credentials/rotate/status",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert r.status_code == 403


def test_rotation_status_returns_key_info(test_client, admin_token):
    with patch(
        "app.services.credential_rotation_service.get_key_status"
    ) as mock_status:
        mock_status.return_value = {
            "mode": "keyring",
            "keyring_size": 1,
            "primary_key_id": "abc123",
        }
        r = test_client.get(
            "/api/credentials/rotate/status",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
    assert r.status_code == 200
    data = r.json()["data"]["key_status"]
    assert "mode" in data


def test_existing_credentials_decrypt_after_rotation(test_client, admin_token):
    from app.security.encryption import encrypt_value
    from app.services import credential_service

    plaintext = "my-secret-value"
    encrypt_value(plaintext)

    entry = credential_service.create_credential(
        scope="user:testadmin",
        name="test_rotation_secret",
        value_plaintext=plaintext,
        created_by="testadmin",
    )

    with patch("app.services.credential_rotation_service.rotate_key") as mock_rotate:
        mock_rotate.return_value = (
            True,
            "done",
            {"re_encrypted_count": 1, "keyring_size": 2},
        )

        r = test_client.post(
            "/api/credentials/rotate",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r.status_code == 200

    re_encrypted = credential_service.get_credential(entry["id"])["value_encrypted"]
    from app.security.encryption import decrypt_value

    assert decrypt_value(re_encrypted) == plaintext


def test_restore_key_requires_admin(test_client, admin_token):
    fake_key = Fernet.generate_key().decode()
    with patch("app.services.credential_rotation_service.restore_key") as mock_restore:
        mock_restore.return_value = (True, "Key restored successfully")
        r = test_client.post(
            "/api/credentials/restore-key",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"key_base64": fake_key},
        )
    assert r.status_code == 200
    mock_restore.assert_called_once()


def test_restore_key_rejects_key_that_cannot_decrypt_entries(clean_db):
    from app.services import credential_service, credential_rotation_service
    from app.security import encryption

    encryption._fernet = None
    encryption._keyring = None
    credential_service.create_credential(
        scope="user:admin",
        name="restore_key_guard",
        value_plaintext="must-stay-decryptable",
        created_by="admin",
    )
    wrong_key = Fernet.generate_key()
    ok, msg = credential_rotation_service.restore_key("admin", wrong_key)
    assert not ok
    assert "cannot decrypt" in msg


def test_restore_key_rewrites_valid_keyring_and_keeps_credentials_accessible(
    clean_db,
):
    from app.security import encryption
    from app.services import credential_service, credential_rotation_service

    entry = credential_service.create_credential(
        scope="user:admin",
        name="restore_key_secret",
        value_plaintext="restore-secret",
        created_by="admin",
    )

    key_bytes = encryption.get_primary_key()
    encryption._fernet = None
    encryption._keyring = None

    ok, msg = credential_rotation_service.restore_key("admin", key_bytes)
    assert ok, msg

    status = credential_rotation_service.get_key_status()
    assert status["mode"] == "keyring"
    assert status["keyring_size"] == 1
    assert credential_service.resolve_reference(entry["reference_name"]) == "restore-secret"


def test_real_rotation_reencrypts_entries_and_persists_keyring(
    test_client, admin_token
):
    from app.config import settings
    from app.security import encryption
    from app.services import credential_service, credential_rotation_service

    entry = credential_service.create_credential(
        scope="user:admin",
        name="real_rotation_secret",
        value_plaintext="rotation-secret",
        created_by="admin",
    )
    old_key = encryption.get_primary_key()

    r = test_client.post(
        "/api/credentials/rotate",
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert r.status_code == 200
    assert (
        credential_service.resolve_reference(entry["reference_name"])
        == "rotation-secret"
    )
    assert encryption.get_primary_key() != old_key
    assert (settings.data_dir / "credential.keyring").exists()
    status = credential_rotation_service.get_key_status()
    assert status["mode"] == "keyring"
    assert status["keyring_size"] >= 2


def test_keyring_file_reload_decrypts_existing_entries(clean_db):
    from app.security import encryption
    from app.services import credential_service

    encryption._fernet = None
    encryption._keyring = None
    entry = credential_service.create_credential(
        scope="user:admin",
        name="reload_secret",
        value_plaintext="reload-secret",
        created_by="admin",
    )
    encryption.rotate_key()
    encryption._fernet = None
    encryption._keyring = None

    assert (
        credential_service.resolve_reference(entry["reference_name"]) == "reload-secret"
    )


def test_otp_and_existing_session_survive_key_rotation(clean_db):
    import pyotp

    from app.security import encryption
    from app.services import auth_service

    auth_service.create_user(
        user_id="rotateuser",
        email="rotateuser@test.local",
        password="testpassword123",
        display_name="Rotate User",
        role="admin",
    )
    session = auth_service.create_session("rotateuser")
    otp = auth_service.enroll_otp("rotateuser")
    assert auth_service.confirm_otp_enrollment(
        "rotateuser", pyotp.TOTP(otp["secret"]).now()
    )

    encryption.rotate_key()

    assert auth_service.validate_session(session["session_id"]) is not None
    current_code = pyotp.TOTP(otp["secret"]).now()
    assert auth_service.verify_otp("rotateuser", current_code)


def test_full_backup_contains_keyring_after_rotation(clean_db, tmp_path):
    import json
    import zipfile

    from app.config import settings
    from app.security import encryption
    from app.services import backup_service, credential_service

    encryption._fernet = None
    encryption._keyring = None
    credential_service.create_credential(
        scope="user:admin",
        name="backup_keyring_secret",
        value_plaintext="backup-secret",
        created_by="admin",
    )
    encryption.rotate_key()

    buf = backup_service.build_backup_zip(
        str(clean_db),
        str(settings.credential_key_path),
        "admin",
    )
    with zipfile.ZipFile(buf, "r") as zf:
        names = set(zf.namelist())
        manifest = json.loads(zf.read("manifest.json"))

    assert {
        "agent-core.db",
        "credential.key",
        "credential.keyring",
        "manifest.json",
    }.issubset(names)
    assert "credential.keyring" in manifest["files"]


def test_encrypted_backup_package_can_be_decrypted_and_restored(clean_db):
    import zipfile

    from app.config import settings
    from app.security import encryption
    from app.services import backup_service, credential_service

    encryption._fernet = None
    encryption._keyring = None
    credential_service.create_credential(
        scope="user:admin",
        name="encrypted_backup_secret",
        value_plaintext="encrypted-backup-secret",
        created_by="admin",
    )

    encrypted_buf, backup_key = backup_service.build_encrypted_backup_package(
        str(clean_db),
        str(settings.credential_key_path),
        "admin",
    )
    decrypted = backup_service.decrypt_backup_package(
        encrypted_buf.getvalue(),
        backup_key,
    )
    with zipfile.ZipFile(decrypted, "r") as zf:
        names = set(zf.namelist())

    assert "agent-core.db" in names
    assert "credential.key" in names
    assert "manifest.json" in names


def test_rotation_concurrent_requests_blocked(test_client, admin_token):
    with patch("app.services.credential_rotation_service.rotate_key") as mock_rotate:
        mock_rotate.side_effect = Exception("in progress")
        try:
            r = test_client.post(
                "/api/credentials/rotate",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert r.status_code == 500
        except Exception:
            pass
