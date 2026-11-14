import logging
import sqlite3
import time
from datetime import timedelta
from typing import Optional

import bcrypt
import pyotp
import jwt
from jwt.exceptions import InvalidTokenError

from app.branding import APP_ISSUER
from app.database import get_db
from app.models.enums import USER_ROLES
from app.services import cleanup_service
from app.time_utils import parse_utc_datetime, utc_now, utc_now_iso

logger = logging.getLogger(__name__)


BCRYPT_COST = 12


class RegistrationDisabledError(RuntimeError):
    """Raised when the one-time bootstrap administrator already exists."""


# Session tokens are signed with their own random key, kept in the data
# directory next to the database. It never touches stored credentials, and a
# credential-key rotation leaves sessions alone. Deleting the file and
# restarting signs everyone out.
SESSION_KEY_FILENAME = "session.key"
_session_key: Optional[bytes] = None


def _session_signing_key() -> bytes:
    global _session_key
    if _session_key is not None:
        return _session_key
    import os
    import secrets
    import tempfile

    from app.config import settings

    path = settings.data_dir / SESSION_KEY_FILENAME
    if not path.exists():
        # Write the new key under a temporary name, then link it into place.
        # link() fails when the file exists, so if two workers start at once
        # one key wins, and neither ever reads a half-written file.
        candidate = secrets.token_bytes(32)
        descriptor, temporary = tempfile.mkstemp(prefix="session.key.tmp-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as output:
                os.fchmod(output.fileno(), 0o600)
                output.write(candidate)
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
        finally:
            os.unlink(temporary)
    key = path.read_bytes()
    if len(key) < 32:
        raise RuntimeError(f"{path} is too short to be a session key; delete it to make a new one")
    _session_key = key
    return key


def reset_session_key_cache() -> None:
    global _session_key
    _session_key = None


def create_jwt(session_id: str) -> str:
    return jwt.encode({"sub": session_id}, _session_signing_key(), algorithm="HS256")


def decode_jwt(token: str) -> Optional[str]:
    try:
        payload = jwt.decode(token, _session_signing_key(), algorithms=["HS256"])
    except InvalidTokenError:
        return None
    return payload.get("sub")


def hash_password(password: str) -> str:
    return bcrypt.hashpw(
        password.encode("utf-8"), bcrypt.gensalt(rounds=BCRYPT_COST)
    ).decode("utf-8")


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
        conn.execute(
            """
            INSERT INTO users (id, email, password_hash, display_name, role, is_active)
            VALUES (?, ?, ?, ?, ?, 1)
            """,
            (user_id, email, password_hash, display_name, role),
        )
        conn.commit()
        return {
            "id": user_id,
            "email": email,
            "display_name": display_name,
            "role": role,
            "is_active": True,
        }


def create_initial_admin(
    user_id: str,
    email: str,
    password: str,
    display_name: str,
) -> dict:
    """Create the installation's first user atomically.

    Hashing happens before the write lock so bcrypt does not stall unrelated
    database work. The user-count check and insert then share one immediate
    transaction, so concurrent bootstrap requests cannot both become admins.
    """
    password_hash = hash_password(password)

    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT 1 FROM users LIMIT 1").fetchone()
        if row:
            raise RegistrationDisabledError("User registration is disabled")
        conn.execute(
            """
            INSERT INTO users (id, email, password_hash, display_name, role, is_active)
            VALUES (?, ?, ?, ?, 'admin', 1)
            """,
            (user_id, email, password_hash, display_name),
        )
        return {
            "id": user_id,
            "email": email,
            "display_name": display_name,
            "role": "admin",
            "is_active": True,
        }


def get_user_by_email(email: str) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, email, password_hash, display_name, role, is_active FROM users WHERE email = ?",
            (email,),
        ).fetchone()
        return dict(row) if row else None


def get_user_by_id(user_id: str) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, email, display_name, role, timezone, is_active FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None


def update_user_timezone(user_id: str, timezone: Optional[str]) -> bool:
    """Set (or clear, with None) a user's display timezone. Validates against the
    IANA database so only real zone names are stored."""
    if timezone is not None:
        from zoneinfo import available_timezones

        if timezone not in available_timezones():
            raise ValueError(f"Unknown timezone: {timezone}")
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE users SET timezone = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (timezone, user_id),
        )
        conn.commit()
        return cursor.rowcount > 0


def count_users() -> int:
    with get_db() as conn:
        row = conn.execute("SELECT COUNT(*) as count FROM users").fetchone()
        return row["count"] if row else 0


def list_users() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT u.id, u.email, u.display_name, u.role, u.is_active, u.created_at,
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
    is_active: Optional[bool] = None,
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
    if is_active is not None:
        updates.append("is_active = ?")
        values.append(1 if is_active else 0)

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
            if is_active is False:
                conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            conn.commit()
        except Exception:
            return False, "User could not be updated"
        return cursor.rowcount > 0, ""


def delete_user(user_id: str) -> tuple[bool, str]:
    """Remove a user and everything that belonged only to them.

    Ordering matters: foreign keys are enforced, so dependants are cleared
    before the rows they point at. Any constraint still left unsatisfied is
    reported rather than raised — a purge that cannot complete should say so,
    not surface as a 500 after it has already deleted half of someone's data.
    """
    try:
        return _delete_user(user_id)
    except sqlite3.IntegrityError as exc:
        logger.exception("Purging user %s hit a constraint", user_id)
        return False, f"User could not be fully removed: {exc}"


def _delete_user(user_id: str) -> tuple[bool, str]:
    with get_db() as conn:
        user_row = conn.execute(
            "SELECT id FROM users WHERE id = ?", (user_id,)
        ).fetchone()
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
        # Grants this user held on workspaces they do not own, and grants they
        # issued on workspaces that outlive them. Both columns reference
        # users(id) without a cascade, so either one left behind aborts the
        # delete on the last statement — after everything above has already
        # been removed.
        conn.execute(
            "DELETE FROM workspace_collaborators WHERE user_id = ?", (user_id,)
        )
        conn.execute(
            "UPDATE workspace_collaborators SET created_by = NULL WHERE created_by = ?",
            (user_id,),
        )
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
        user = conn.execute(
            "SELECT is_active FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if not user or not user["is_active"]:
            raise ValueError("Cannot create a session for an inactive user")
        conn.execute(
            """
            INSERT INTO sessions (id, user_id, expires_at, channel)
            VALUES (?, ?, ?, ?)
            """,
            (session_id, user_id, expires_at, channel),
        )
        conn.commit()

    jwt_token = create_jwt(session_id)
    return {
        "session_id": jwt_token,
        "db_session_id": session_id,
        "expires_at": expires_at,
    }


def get_session(session_id: str) -> Optional[dict]:
    with get_db() as conn:
        cursor = conn.execute(
            """
            SELECT s.id, s.user_id, s.expires_at, s.last_activity, s.channel,
                   u.email, u.display_name, u.role, u.timezone, u.is_active
            FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.id = ? AND u.is_active = 1
            """,
            (session_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def validate_session(session_token: str, inactivity_minutes: int = 30) -> Optional[dict]:
    if session_token.startswith(OPERATOR_TOKEN_PREFIX):
        # An operator token stands in for its person wherever a session would.
        return validate_operator_token(session_token)
    session_id = decode_jwt(session_token)
    if not session_id:
        return None

    session = get_session(session_id)
    if not session:
        return None

    if session["channel"] != "dashboard":
        # A half-finished sign-in (pending_otp) never authenticates, and neither does a leftover
        # row from the removed trusted-device feature (channel "approvals").
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
                    user_id, secret_encrypted, pending_secret_encrypted, is_active, pending_at
                )
                VALUES (?, ?, ?, 0, CURRENT_TIMESTAMP)
                """,
                (user_id, encrypted_secret, encrypted_secret),
            )
        conn.commit()

    totp = pyotp.TOTP(secret)
    otp_uri = totp.provisioning_uri(name=user_id, issuer_name=APP_ISSUER)

    return {
        "secret": secret,
        "otp_uri": otp_uri,
    }


def confirm_otp_enrollment(user_id: str, otp_code: str) -> bool:
    secret_data = get_otp_secret(user_id)
    if not secret_data:
        return False

    pending_secret = (
        secret_data.get("pending_secret_encrypted") or secret_data["secret_encrypted"]
    )
    secret = decrypt_otp_secret(pending_secret)
    totp = pyotp.TOTP(secret)
    if not totp.verify(otp_code, valid_window=1):
        return False

    with get_db() as conn:
        conn.execute(
            """
            UPDATE otp_secrets
            SET secret_encrypted = ?,
                pending_secret_encrypted = NULL,
                pending_at = NULL,
                is_active = 1,
                enrolled_at = CURRENT_TIMESTAMP,
                last_used = ?
            WHERE user_id = ?
            """,
            (pending_secret, utc_now_iso(), user_id),
        )
        conn.commit()

    return True


def verify_otp(user_id: str, otp_code: str) -> bool:
    secret_data = get_otp_secret(user_id)
    if not secret_data or not secret_data.get("is_active"):
        return False

    secret = decrypt_otp_secret(secret_data["secret_encrypted"])
    totp = pyotp.TOTP(secret)

    if not totp.verify(otp_code, valid_window=1):
        return False
    # A code is good for one login: record the time step it belongs to and
    # refuse that step (or an earlier one) again, so a code seen in transit
    # cannot be replayed inside the validity window.
    now = time.time()
    step = next(
        (int((now + offset * totp.interval) // totp.interval)
         for offset in (0, -1, 1)
         if totp.verify(otp_code, for_time=now + offset * totp.interval, valid_window=0)),
        int(now // totp.interval),
    )
    with get_db() as conn:
        updated = conn.execute(
            "UPDATE otp_secrets SET last_used = ?, last_used_step = ? "
            "WHERE user_id = ? AND (last_used_step IS NULL OR last_used_step < ?)",
            (utc_now_iso(), step, user_id, step),
        ).rowcount
        conn.commit()
    return updated == 1


def get_otp_secret(user_id: str) -> Optional[dict]:
    with get_db() as conn:
        cursor = conn.execute(
            "SELECT secret_encrypted, pending_secret_encrypted, is_active FROM otp_secrets WHERE user_id = ?",
            (user_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def is_otp_enrolled(user_id: str) -> bool:
    secret_data = get_otp_secret(user_id)
    return bool(secret_data is not None and secret_data.get("is_active"))


def encrypt_otp_secret(secret: str) -> str:
    from app.security.encryption import encrypt_value

    return encrypt_value(secret)


def decrypt_otp_secret(encrypted: str) -> str:
    from app.security.encryption import decrypt_value

    return decrypt_value(encrypted)


def change_password(
    user_id: str, current_password: str, new_password: str
) -> tuple[bool, str]:
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


def disable_otp(user_id: str) -> bool:
    with get_db() as conn:
        cursor = conn.execute("DELETE FROM otp_secrets WHERE user_id = ?", (user_id,))
        conn.commit()
        return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# Operator tokens
#
# A login session token works as a Bearer key only on /api/auth/*. Scripts that
# must act as a person elsewhere (attended admin scripts that create grants or
# credentials under that person's authority) use an operator token instead: the
# person mints it while signed in, it lasts minutes, is shown once, and can be
# revoked. Requests made with it are audited as the person.

OPERATOR_TOKEN_PREFIX = "ac_op_"
OPERATOR_TOKEN_MIN_MINUTES = 5
OPERATOR_TOKEN_MAX_MINUTES = 60
OPERATOR_TOKEN_DEFAULT_MINUTES = 30


def _operator_token_hash(token: str) -> str:
    import hashlib

    return hashlib.sha256(token.encode()).hexdigest()


def create_operator_token(user_id: str, minutes: int = OPERATOR_TOKEN_DEFAULT_MINUTES,
                          label: Optional[str] = None) -> tuple[str, dict]:
    """Mint a token for `user_id`. Returns (token, record); the token is never stored."""
    import secrets
    import uuid

    minutes = int(minutes)
    if not OPERATOR_TOKEN_MIN_MINUTES <= minutes <= OPERATOR_TOKEN_MAX_MINUTES:
        raise ValueError(
            f"minutes must be between {OPERATOR_TOKEN_MIN_MINUTES} and {OPERATOR_TOKEN_MAX_MINUTES}"
        )
    label = (label or "").strip()[:100] or None
    token = OPERATOR_TOKEN_PREFIX + secrets.token_urlsafe(32)
    now = utc_now()
    record = {
        "id": uuid.uuid4().hex,
        "label": label,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=minutes)).isoformat(),
    }
    with get_db() as conn:
        conn.execute(
            "INSERT INTO operator_tokens (id, user_id, token_hash, label, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (record["id"], user_id, _operator_token_hash(token), label,
             record["created_at"], record["expires_at"]),
        )
        conn.commit()
    return token, record


def validate_operator_token(token: str) -> Optional[dict]:
    """The session-shaped identity behind a live operator token, or None."""
    if not token.startswith(OPERATOR_TOKEN_PREFIX):
        return None
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT t.id AS token_id, t.expires_at, t.revoked_at, u.id AS user_id,
                   u.email, u.display_name, u.role, u.timezone, u.is_active
            FROM operator_tokens t JOIN users u ON u.id = t.user_id
            WHERE t.token_hash = ? AND u.is_active = 1
            """,
            (_operator_token_hash(token),),
        ).fetchone()
        if not row or row["revoked_at"] or utc_now() > parse_utc_datetime(row["expires_at"]):
            return None
        conn.execute("UPDATE operator_tokens SET last_used_at = ? WHERE id = ?",
                     (utc_now_iso(), row["token_id"]))
        conn.commit()
    session = dict(row)
    session.update({"id": f"operator:{row['token_id']}", "channel": "operator",
                    "operator_token_id": row["token_id"], "last_activity": utc_now_iso()})
    session.pop("revoked_at", None)
    session.pop("token_id", None)
    return session


def list_operator_tokens(user_id: str) -> list[dict]:
    """The user's tokens that have not expired or been revoked, newest first."""
    now = utc_now()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, label, created_at, expires_at, last_used_at FROM operator_tokens "
            "WHERE user_id = ? AND revoked_at IS NULL ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows if parse_utc_datetime(r["expires_at"]) > now]


OPERATOR_TOKEN_KEEP_DAYS = 395  # the audit window (13 months): an old token row is only history after that


def prune_sign_ins(*, inactivity_minutes: Optional[int] = None) -> dict:
    """Housekeeping for rows that only pile up: dead sessions and spent operator tokens.

    A session past its expiry, or idle longer than the inactivity limit, can never authenticate again (it
    would be deleted on its next use), so it is deleted now; so is a half-finished sign-in past its expiry.
    An operator token past its expiry is marked revoked, dated at its expiry, so every list shows it the same
    way as one a person revoked; a row older than the audit window is deleted. Returns counts.
    """
    from app.config import settings

    idle = settings.INACTIVITY_TIMEOUT_MINUTES if inactivity_minutes is None else inactivity_minutes
    now = utc_now()
    idle_cutoff = now - timedelta(minutes=idle)
    with get_db() as conn:
        dead = []
        for row in conn.execute("SELECT id, expires_at, last_activity, channel FROM sessions").fetchall():
            try:
                expired = parse_utc_datetime(row["expires_at"]) < now
                idle_out = row["channel"] == "dashboard" and parse_utc_datetime(row["last_activity"]) < idle_cutoff
            except (TypeError, ValueError):
                expired, idle_out = True, False  # an unreadable date can never authenticate
            if expired or idle_out:
                dead.append(row["id"])
        for offset in range(0, len(dead), 500):
            chunk = dead[offset : offset + 500]
            conn.execute(f"DELETE FROM sessions WHERE id IN ({','.join('?' for _ in chunk)})", chunk)

        closed = 0
        for row in conn.execute("SELECT id, expires_at FROM operator_tokens WHERE revoked_at IS NULL").fetchall():
            if parse_utc_datetime(row["expires_at"]) < now:
                conn.execute("UPDATE operator_tokens SET revoked_at = ? WHERE id = ?", (row["expires_at"], row["id"]))
                closed += 1
        old_cutoff = (now - timedelta(days=OPERATOR_TOKEN_KEEP_DAYS)).isoformat()
        removed = conn.execute(
            "DELETE FROM operator_tokens WHERE expires_at < ? AND revoked_at IS NOT NULL", (old_cutoff,)
        ).rowcount
        conn.commit()
    return {"sessions_pruned": len(dead), "operator_tokens_closed": closed, "operator_tokens_pruned": removed or 0}


def revoke_operator_token(user_id: str, token_id: str) -> bool:
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE operator_tokens SET revoked_at = ? WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
            (utc_now_iso(), token_id, user_id),
        )
        conn.commit()
        return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# CSRF
#
# Requests authenticated by the session cookie that change state must carry
# X-CSRF-Token. The token is derived from the session id with the session key,
# so it needs no storage and is known to every page rendered for the session.

CSRF_HEADER = "X-CSRF-Token"


def csrf_token_for_session_id(session_id: str) -> str:
    import hashlib
    import hmac

    return hmac.new(_session_signing_key(), b"csrf:" + session_id.encode(), hashlib.sha256).hexdigest()


def csrf_token_for_session_token(session_token: str) -> Optional[str]:
    session_id = decode_jwt(session_token) if session_token else None
    return csrf_token_for_session_id(session_id) if session_id else None

