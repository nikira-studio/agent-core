import logging
import re
import secrets
import sqlite3
from typing import Optional

from app.branding import CREDENTIAL_PREFIX
from app.connectors.base import Credential
from app.database import get_db
from app.security.encryption import encrypt_value, decrypt_value
from app.models.enums import normalize_id
from app.time_utils import parse_utc_datetime, utc_now

logger = logging.getLogger(__name__)


def _generate_unique_suffix() -> str:
    return secrets.token_hex(2).upper()


def _build_reference_name(name: str) -> str:
    name_clean = re.sub(r"[^A-Z0-9]", "_", name.upper().strip())
    name_clean = re.sub(r"_+", "_", name_clean).strip("_")
    if len(name_clean) > 20:
        name_clean = name_clean[:20]

    base = (
        f"{CREDENTIAL_PREFIX}{name_clean}"
        if name_clean
        else f"{CREDENTIAL_PREFIX}VAULT"
    )
    return f"{base}_{_generate_unique_suffix()}"


def create_credential(
    scope: str,
    name: str,
    value_plaintext: str,
    label: Optional[str] = None,
    metadata_json: Optional[str] = None,
    expires_at: Optional[str] = None,
    created_by: Optional[str] = None,
) -> dict:
    parts = scope.split(":", 1)
    if len(parts) == 2 and parts[0] in ("user", "agent", "workspace"):
        normalized_scope = f"{parts[0]}:{normalize_id(parts[1])}"
    else:
        normalized_scope = scope
    entry_id = secrets.token_urlsafe(16)
    value_encrypted = encrypt_value(value_plaintext)

    with get_db() as conn:
        for _ in range(10):
            reference_name = _build_reference_name(name)
            try:
                conn.execute(
                    """
                    INSERT INTO credentials
                    (id, scope, name, label, value_encrypted,
                     metadata_json, expires_at, reference_name, created_by)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        entry_id,
                        normalized_scope,
                        name,
                        label,
                        value_encrypted,
                        metadata_json,
                        expires_at,
                        reference_name,
                        created_by,
                    ),
                )
                conn.commit()
                return {
                    "id": entry_id,
                    "scope": normalized_scope,
                    "name": name,
                    "label": label,
                    "metadata_json": metadata_json,
                    "expires_at": expires_at,
                    "reference_name": reference_name,
                    "created_by": created_by,
                }
            except sqlite3.IntegrityError as exc:
                if "reference_name" not in str(exc):
                    raise
        raise RuntimeError("Could not generate a unique credential reference name")


def get_credential(entry_id: str) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, scope, name, label, value_encrypted, metadata_json, "
            "expires_at, reference_name, created_by, created_at, updated_at "
            "FROM credentials WHERE id = ?",
            (entry_id,),
        ).fetchone()
        return dict(row) if row else None


def get_credential_by_reference(reference_name: str) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, scope, name, label, value_encrypted, metadata_json, "
            "expires_at, reference_name, created_by, created_at, updated_at "
            "FROM credentials WHERE reference_name = ?",
            (reference_name,),
        ).fetchone()
        return dict(row) if row else None


def list_credentials(
    scope: Optional[str] = None, limit: int = 50, offset: int = 0
) -> list[dict]:
    with get_db() as conn:
        query = (
            "SELECT id, scope, name, label, metadata_json, "
            "expires_at, reference_name, created_by, created_at "
            "FROM credentials WHERE 1=1"
        )
        params = []
        if scope:
            query += " AND scope = ?"
            params.append(scope)
        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]


def update_credential(entry_id: str, **fields) -> bool:
    allowed = (
        "name",
        "label",
        "value_encrypted",
        "metadata_json",
        "expires_at",
    )
    # `None` normally means "caller did not supply this", which is why it is
    # skipped. `expires_at` is the exception: an operator clearing the expiry
    # field is asking for it to be removed, and there is no other way to say so.
    nullable = ("expires_at",)
    updates = []
    params = []
    for key, val in fields.items():
        if key in allowed and (val is not None or key in nullable):
            if key == "name" and not str(val).strip():
                return False
            updates.append(f"{key} = ?")
            params.append(val)
    if not updates:
        return False
    updates.append("updated_at = CURRENT_TIMESTAMP")
    params.append(entry_id)
    with get_db() as conn:
        cursor = conn.execute(
            f"UPDATE credentials SET {', '.join(updates)} WHERE id = ?",
            params,
        )
        conn.commit()
        return cursor.rowcount > 0


def delete_credential(entry_id: str) -> bool:
    with get_db() as conn:
        conn.execute(
            "UPDATE connector_bindings SET credential_id = NULL WHERE credential_id = ?",
            (entry_id,),
        )
        cursor = conn.execute("DELETE FROM credentials WHERE id = ?", (entry_id,))
        conn.commit()
        return cursor.rowcount > 0


def is_expired(entry: dict) -> bool:
    """Whether a stored credential should still be handed out.

    An unreadable expiry fails closed. Writes are validated now, but a row
    written before that validation existed must not be able to turn a resolve
    into a 500 — and of the two ways to be wrong, refusing a secret is the
    recoverable one.
    """
    expires_at = entry.get("expires_at")
    if not expires_at:
        return False
    try:
        return utc_now() > parse_utc_datetime(expires_at)
    except (ValueError, TypeError):
        logger.warning(
            "Credential %s has an unreadable expires_at (%r); treating it as expired",
            entry.get("id"),
            expires_at,
        )
        return True


def resolve_reference(reference_name: str) -> Optional[str]:
    entry = get_credential_by_reference(reference_name)
    if not entry:
        return None
    if is_expired(entry):
        return None
    return decrypt_value(entry["value_encrypted"])


def resolve_credential(reference_name: str) -> Optional[Credential]:
    entry = get_credential_by_reference(reference_name)
    if not entry:
        return None
    if is_expired(entry):
        return None
    return Credential.from_resolved(decrypt_value(entry["value_encrypted"]), entry)


def update_credential_value(reference_name: str, new_plaintext: str) -> bool:
    entry = get_credential_by_reference(reference_name)
    if not entry:
        return False
    return update_credential(entry["id"], value_encrypted=encrypt_value(new_plaintext))


def get_credential_scopes() -> list[str]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT scope FROM credentials ORDER BY scope"
        ).fetchall()
        return [row["scope"] for row in rows]
