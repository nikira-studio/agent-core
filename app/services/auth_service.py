import secrets
import hashlib
from datetime import datetime, timedelta
from typing import Optional

import bcrypt
import pyotp

from app.database import get_db
from app.models.enums import USER_ROLES
from app.security.response_helpers import error_response, success_response
from jose import jwt
from app.services import cleanup_service
from app.time_utils import parse_utc_datetime, utc_now, utc_now_iso


BCRYPT_COST = 12


def create_jwt(session_id: str) -> str:
    from app.config import settings
    if settings.ENCRYPTION_KEY == "auto":
        from app.security.encryption import get_primary_key
        key = get_primary_key()
    else:
        key = settings.ENCRYPTION_KEY.encode()
    return jwt.encode({"sub": session_id}, key, algorithm="HS256")


def decode_jwt(token: str) -> Optional[str]:
    from app.config import settings
    if settings.ENCRYPTION_KEY == "auto":
        from app.security.encryption import get_keyring
        keys = get_keyring()
    else:
        keys = [settings.ENCRYPTION_KEY.encode()]
    for key in keys:
        try:
            payload = jwt.decode(token, key, algorithms=["HS256"])
            return payload.get("sub")
        except Exception:
            continue
    return None


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=BCRYPT_COST)).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def create_user(
    user_id: str,
    email: str,
    password: str,
    display_name: str,
    role: str = "user",
) -> dict:
    password_hash = hash_password(password)

    with get_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO users (id, email, password_hash, display_name, role)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, email, password_hash, display_name, role),
        )
        conn.commit()
        return {"id": user_id, "email": email, "display_name": display_name, "role": role}


def get_user_by_email(email: str) -> Optional[dict]:
    with get_db() as conn:
        cursor = conn.execute(
            "SELECT id, email, password_hash, display_name, role FROM users WHERE email = ?",
            (email,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def get_user_by_id(user_id: str) -> Optional[dict]:
    with get_db() as conn:
        cursor = conn.execute(
            "SELECT id, email, display_name, role FROM users WHERE id = ?",
            (user_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def count_users() -> int:
    with get_db() as conn:
        cursor = conn.execute("SELECT COUNT(*) as count FROM users")
        row = cursor.fetchone()
        return row["count"] if row else 0


def list_users() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT u.id, u.email, u.display_name, u.role, u.created_at,
                   CASE WHEN o.user_id IS NOT NULL THEN 1 ELSE 0 END as otp_enrolled
            FROM users u
            LEFT JOIN otp_secrets o ON o.user_id = u.id AND o.is_active = 1
            ORDER BY u.created_at ASC
            """
        ).fetchall()
        return [dict(r) for r in rows]


def update_user(
    user_id: str,
    *,
    email: Optional[str] = None,
    display_name: Optional[str] = None,
    role: Optional[str] = None,
    password: Optional[str] = None,
) -> tuple[bool, str]:
    updates = []
    values = []

    if email is not None:
        updates.append("email = ?")
        values.append(email)
    if display_name is not None:
        updates.append("display_name = ?")
        values.append(display_name)
    if role is not None:
        if role not in USER_ROLES:
            return False, "Invalid role"
        updates.append("role = ?")
        values.append(role)
    if password:
        updates.append("password_hash = ?")
        values.append(hash_password(password))

    if not updates:
        return True, ""

    updates.append("updated_at = ?")
    values.append(utc_now_iso())
    values.append(user_id)

    with get_db() as conn:
        try:
            cursor = conn.execute(
                f"UPDATE users SET {', '.join(updates)} WHERE id = ?",
                tuple(values),
            )
            conn.commit()
        except Exception:
            return False, "User could not be updated"
        return cursor.rowcount > 0, ""


def delete_user(user_id: str) -> tuple[bool, str]:
    with get_db() as conn:
        user_row = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user_row:
            return False, ""

        agent_rows = conn.execute(
            "SELECT id FROM agents WHERE owner_user_id = ?", (user_id,)
        ).fetchall()
        workspace_rows = conn.execute(
            "SELECT id FROM workspaces WHERE owner_user_id = ?", (user_id,)
        ).fetchall()

        for workspace in workspace_rows:
            workspace_scope = f"workspace:{workspace['id']}"
            cleanup_service.delete_scope_data(conn, workspace_scope)
            conn.execute("DELETE FROM workspaces WHERE id = ?", (workspace["id"],))

        for agent in agent_rows:
            agent_scope = f"agent:{agent['id']}"
            cleanup_service.delete_scope_data(conn, agent_scope)
            conn.execute(
                """
                DELETE FROM agent_activity
                WHERE agent_id = ? OR assigned_agent_id = ? OR reassigned_from_agent_id = ?
                """,
                (agent["id"], agent["id"], agent["id"]),
            )
            conn.execute("DELETE FROM agents WHERE id = ?", (agent["id"],))

        cleanup_service.delete_scope_data(conn, f"user:{user_id}")
        conn.execute(
            "UPDATE agents SET default_user_id = owner_user_id, updated_at = CURRENT_TIMESTAMP WHERE default_user_id = ?",
            (user_id,),
        )
        conn.execute("DELETE FROM agent_activity WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM otp_secrets WHERE user_id = ?", (user_id,))
        cursor = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
        return cursor.rowcount > 0, ""


def create_session(
    user_id: str,
    channel: str = "dashboard",
    expiry_hours: int = 8,
) -> dict:
    import uuid

    session_id = str(uuid.uuid4())
    expires_at = (utc_now() + timedelta(hours=expiry_hours)).isoformat()

    with get_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO sessions (id, user_id, expires_at, channel)
            VALUES (?, ?, ?, ?)
            """,
            (session_id, user_id, expires_at, channel),
        )
        conn.commit()
    
    jwt_token = create_jwt(session_id)
    return {"session_id": jwt_token, "db_session_id": session_id, "expires_at": expires_at}


def get_session(session_id: str) -> Optional[dict]:
    with get_db() as conn:
        cursor = conn.execute(
            """
            SELECT s.id, s.user_id, s.expires_at, s.last_activity, s.channel,
                   u.email, u.display_name, u.role
            FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.id = ?
            """,
            (session_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def validate_session(session_token: str, inactivity_minutes: int = 30) -> Optional[dict]:
    session_id = decode_jwt(session_token)
    if not session_id:
        return None

    session = get_session(session_id)
    if not session:
        return None

    if session["channel"] == "pending_otp":
        return None

    expires_at = parse_utc_datetime(session["expires_at"])
    if utc_now() > expires_at:
        delete_session(session_id)
        return None

    last_activity = parse_utc_datetime(session["last_activity"])
    if (utc_now() - last_activity).total_seconds() > (inactivity_minutes * 60):
        delete_session(session_id)
        return None

    update_session_activity(session_id)
    return session


def update_session_activity(session_id: str) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE sessions SET last_activity = ? WHERE id = ?",
            (utc_now_iso(), session_id),
        )
        conn.commit()


def delete_session(session_id: str) -> None:
    with get_db() as conn:
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        conn.commit()


def enroll_otp(user_id: str) -> dict:
    secret = pyotp.random_base32()
    encrypted_secret = encrypt_otp_secret(secret)

    with get_db() as conn:
        existing = conn.execute(
            "SELECT user_id FROM otp_secrets WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE otp_secrets
                SET pending_secret_encrypted = ?, pending_at = CURRENT_TIMESTAMP
                WHERE user_id = ?
                """,
                (encrypted_secret, user_id),
            )
        else:
            conn.execute(
                """
                INSERT INTO otp_secrets (
                    user_id, secret_encrypted, pending_secret_encrypted, backup_codes_json, is_active, pending_at
                )
                VALUES (?, ?, ?, ?, 0, CURRENT_TIMESTAMP)
                """,
                (user_id, encrypted_secret, encrypted_secret, "[]"),
            )
        conn.commit()

    totp = pyotp.TOTP(secret)
    otp_uri = totp.provisioning_uri(name=user_id, issuer_name="AgentCore")

    return {
        "secret": secret,
        "otp_uri": otp_uri,
    }


def _generate_backup_codes() -> tuple[list[str], list[str]]:
    backup_codes = [secrets.token_hex(16) for _ in range(10)]
    backup_codes_hashed = [hashlib.sha256(code.encode()).hexdigest() for code in backup_codes]
    return backup_codes, backup_codes_hashed


def confirm_otp_enrollment(user_id: str, otp_code: str) -> Optional[list[str]]:
    secret_data = get_otp_secret(user_id)
    if not secret_data:
        return None

    pending_secret = secret_data.get("pending_secret_encrypted") or secret_data["secret_encrypted"]
    secret = decrypt_otp_secret(pending_secret)
    totp = pyotp.TOTP(secret)
    if not totp.verify(otp_code, valid_window=1):
        return None

    backup_codes, backup_codes_hashed = _generate_backup_codes()

    import json
    with get_db() as conn:
        conn.execute(
            """
            UPDATE otp_secrets
            SET secret_encrypted = ?,
                pending_secret_encrypted = NULL,
                pending_at = NULL,
                backup_codes_json = ?,
                is_active = 1,
                enrolled_at = CURRENT_TIMESTAMP,
                last_used = ?
            WHERE user_id = ?
            """,
            (pending_secret, json.dumps(backup_codes_hashed), utc_now_iso(), user_id),
        )
        conn.commit()

    return backup_codes


def verify_otp(user_id: str, otp_code: str) -> bool:
    secret_data = get_otp_secret(user_id)
    if not secret_data or not secret_data.get("is_active"):
        return False

    secret = decrypt_otp_secret(secret_data["secret_encrypted"])
    totp = pyotp.TOTP(secret)

    if totp.verify(otp_code, valid_window=1):
        with get_db() as conn:
            conn.execute(
                "UPDATE otp_secrets SET last_used = ? WHERE user_id = ?",
                (utc_now_iso(), user_id),
            )
            conn.commit()
        return True

    return False


def verify_backup_code(user_id: str, backup_code: str) -> bool:
    secret_data = get_otp_secret(user_id)
    if not secret_data or not secret_data.get("is_active"):
        return False

    import json
    backup_codes_hashed = json.loads(secret_data["backup_codes_json"] or "[]")
    provided_hash = hashlib.sha256(backup_code.encode()).hexdigest()

    for i, stored_hash in enumerate(backup_codes_hashed):
        if secrets.compare_digest(provided_hash, stored_hash):
            new_codes = backup_codes_hashed.copy()
            del new_codes[i]

            with get_db() as conn:
                conn.execute(
                    "UPDATE otp_secrets SET backup_codes_json = ? WHERE user_id = ?",
                    (json.dumps(new_codes), user_id),
                )
                conn.commit()
            return True

    return False


def verify_otp_or_backup_code(user_id: str, otp_or_backup_code: str) -> bool:
    if verify_otp(user_id, otp_or_backup_code):
        return True
    return verify_backup_code(user_id, otp_or_backup_code)


def get_otp_secret(user_id: str) -> Optional[dict]:
    with get_db() as conn:
        cursor = conn.execute(
            "SELECT secret_encrypted, pending_secret_encrypted, backup_codes_json, is_active FROM otp_secrets WHERE user_id = ?",
            (user_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def is_otp_enrolled(user_id: str) -> bool:
    secret_data = get_otp_secret(user_id)
    return bool(secret_data is not None and secret_data.get("is_active"))


def encrypt_otp_secret(secret: str) -> str:
    from app.config import settings

    if settings.ENCRYPTION_KEY == "auto":
        from app.security.encryption import encrypt_value
        return encrypt_value(secret)
    else:
        from cryptography.fernet import Fernet
        key = settings.ENCRYPTION_KEY.encode()
        f = Fernet(key)
        return f.encrypt(secret.encode()).decode()


def decrypt_otp_secret(encrypted: str) -> str:
    from app.config import settings

    if settings.ENCRYPTION_KEY == "auto":
        from app.security.encryption import decrypt_value
        return decrypt_value(encrypted)
    else:
        from cryptography.fernet import Fernet
        key = settings.ENCRYPTION_KEY.encode()
        f = Fernet(key)
        return f.decrypt(encrypted.encode()).decode()


def change_password(user_id: str, current_password: str, new_password: str) -> tuple[bool, str]:
    with get_db() as conn:
        cursor = conn.execute(
            "SELECT password_hash FROM users WHERE id = ?",
            (user_id,),
        )
        row = cursor.fetchone()
        if not row:
            return False, "User not found"
        if not verify_password(current_password, row["password_hash"]):
            return False, "Current password is incorrect"
        new_hash = hash_password(new_password)
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (new_hash, user_id),
        )
        conn.commit()
        return True, ""


def get_backup_codes(user_id: str) -> list[str]:
    return []


def regenerate_backup_codes(user_id: str) -> list[str]:
    import json
    backup_codes, backup_codes_hashed = _generate_backup_codes()

    with get_db() as conn:
        conn.execute(
            "UPDATE otp_secrets SET backup_codes_json = ? WHERE user_id = ?",
            (json.dumps(backup_codes_hashed), user_id),
        )
        conn.commit()

    return backup_codes


def load_or_create_vault_key() -> bytes:
    from app.config import settings
    from cryptography.fernet import Fernet
    import base64

    key_path = settings.vault_key_path
    if key_path.exists():
        return key_path.read_bytes()

    key = Fernet.generate_key()
    key_path.write_bytes(key)
    return key
