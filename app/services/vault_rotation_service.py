import threading
import os
import json
import shutil

from cryptography.fernet import Fernet

from app.config import settings
from app.database import get_db
from app.time_utils import utc_now


_rotation_lock = threading.Lock()
_rotation_in_progress = False


def rotate_vault_key(admin_user_id: str) -> tuple[bool, str, dict]:
    global _rotation_in_progress

    if not _rotation_lock.acquire(blocking=False):
        return False, "Rotation already in progress", {}

    try:
        from app.security.encryption import (
            rotate_vault_key as _do_rotate,
            get_primary_key,
            decrypt_with_key,
        )
        from app.services import audit_service

        backup_dir = settings.data_dir / "backups"
        os.makedirs(backup_dir, exist_ok=True)
        timestamp = utc_now().strftime("%Y%m%d_%H%M%S")
        pre_backup_path = backup_dir / f"vault.pre_rotation.{timestamp}.bak"

        old_primary = get_primary_key()
        with open(pre_backup_path, "wb") as f:
            f.write(old_primary)

        new_key, new_keyring = _do_rotate()

        re_encrypted_count = 0
        with get_db() as conn:
            rows = conn.execute(
                "SELECT id, value_encrypted FROM vault_entries"
            ).fetchall()
            for row in rows:
                old_keyring = [old_primary]
                plaintext = decrypt_with_key(row["value_encrypted"], old_primary)
                if plaintext is None:
                    for k in new_keyring[:-1]:
                        plaintext = decrypt_with_key(row["value_encrypted"], k)
                        if plaintext is not None:
                            break
                if plaintext is None:
                    continue
                from app.security.encryption import encrypt_value
                new_encrypted = encrypt_value(plaintext)
                conn.execute(
                    "UPDATE vault_entries SET value_encrypted = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (new_encrypted, row["id"]),
                )
                re_encrypted_count += 1
            conn.commit()

        audit_service.write_event(
            actor_type="user",
            actor_id=admin_user_id,
            action="vault_key_rotated",
            result="success",
            details={
                "re_encrypted_entries": re_encrypted_count,
                "new_keyring_size": len(new_keyring),
            },
        )

        return True, f"Rotation complete. {re_encrypted_count} entries re-encrypted.", {
            "re_encrypted_count": re_encrypted_count,
            "keyring_size": len(new_keyring),
        }
    except Exception as e:
        return False, f"Rotation failed: {e}", {}
    finally:
        _rotation_lock.release()


def get_vault_key_status() -> dict:
    keyring_path = settings.data_dir / "vault.keyring"
    vault_key_path = settings.vault_key_path

    if not os.path.exists(keyring_path) and os.path.exists(vault_key_path):
        return {"mode": "legacy_single_key", "keyring_size": 0, "primary_key_id": "legacy"}

    if not os.path.exists(keyring_path):
        return {"mode": "unknown", "keyring_size": 0}

    try:
        with open(keyring_path, "rb") as f:
            data = json.load(f)
        keys = data.get("keys", [])
        return {
            "mode": "keyring",
            "keyring_size": len(keys),
            "primary_key_id": keys[0][:8] if keys else "none",
        }
    except Exception:
        return {"mode": "error", "keyring_size": 0}


def restore_vault_key(admin_user_id: str, key_bytes: bytes) -> tuple[bool, str]:
    try:
        fernet = Fernet(key_bytes)
    except Exception:
        return False, "Invalid Fernet key"

    with get_db() as conn:
        rows = conn.execute("SELECT value_encrypted FROM vault_entries").fetchall()
        for row in rows:
            try:
                fernet.decrypt(row["value_encrypted"].encode())
            except Exception:
                return False, "Provided key cannot decrypt all vault entries"

    keyring_path = settings.data_dir / "vault.keyring"
    backup_dir = settings.data_dir / "backups"
    os.makedirs(backup_dir, exist_ok=True)
    timestamp = utc_now().strftime("%Y%m%d_%H%M%S")

    if os.path.exists(keyring_path):
        with open(keyring_path, "rb") as f:
            prev = f.read()
        with open(backup_dir / f"vault.keyring.{timestamp}.bak", "wb") as f:
            f.write(prev)

    with open(keyring_path, "wb") as f:
        json.dump({"keys": [key_bytes.decode() for key_bytes in [key_bytes]]}, f)
    os.chmod(keyring_path, 0o600)

    vault_key_path = settings.vault_key_path
    with open(vault_key_path, "wb") as f:
        f.write(key_bytes)
    os.chmod(vault_key_path, 0o600)

    from app.services import audit_service
    audit_service.write_event(
        actor_type="user",
        actor_id=admin_user_id,
        action="vault_key_restored",
        result="success",
        details={"mode": "manual_key_restore"},
    )

    global _fernet, _keyring
    from app.security import encryption
    encryption._fernet = None
    encryption._keyring = None

    return True, "Vault key restored successfully"
