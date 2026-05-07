import secrets
import re
import sqlite3
from typing import Optional
from app.database import get_db
from app.security.encryption import encrypt_value, decrypt_value
from app.models.enums import normalize_id
from app.time_utils import parse_utc_datetime, utc_now


_AC_SECRET_RE = re.compile(r"^AC_SECRET_[A-Z0-9_]+$")
_UNIQUE_SUFFIX_RE = re.compile(r"^[A-Z0-9]{4}$")


def _generate_unique_suffix() -> str:
    return secrets.token_hex(2).upper()


def _build_reference_name(name: str) -> str:
    name_clean = re.sub(r"[^A-Z0-9]", "_", name.upper().strip())
    name_clean = re.sub(r"_+", "_", name_clean).strip("_")
    if len(name_clean) > 20:
        name_clean = name_clean[:20]

    base = f"AC_SECRET_{name_clean}" if name_clean else f"AC_SECRET_VAULT"
    return f"{base}_{_generate_unique_suffix()}"


def create_vault_entry(
    scope: str,
    name: str,
    value_plaintext: str,
    label: Optional[str] = None,
    value_type: str = "other",
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
                    INSERT INTO vault_entries
                    (id, scope, name, label, value_encrypted, value_type,
                     metadata_json, expires_at, reference_name, created_by)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (entry_id, normalized_scope, name, label, value_encrypted, value_type,
                     metadata_json, expires_at, reference_name, created_by),
                )
                conn.commit()
                return {
                    "id": entry_id,
                    "scope": normalized_scope,
                    "name": name,
                    "label": label,
                    "value_type": value_type,
                    "metadata_json": metadata_json,
                    "expires_at": expires_at,
                    "reference_name": reference_name,
                    "created_by": created_by,
                }
            except sqlite3.IntegrityError as exc:
                if "reference_name" not in str(exc):
                    raise
        raise RuntimeError("Could not generate a unique vault reference name")


def get_vault_entry(entry_id: str) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, scope, name, label, value_encrypted, value_type, metadata_json, "
            "expires_at, reference_name, created_by, created_at, updated_at "
            "FROM vault_entries WHERE id = ?",
            (entry_id,),
        ).fetchone()
        return dict(row) if row else None


def get_vault_entry_by_reference(reference_name: str) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, scope, name, label, value_encrypted, value_type, metadata_json, "
            "expires_at, reference_name, created_by, created_at, updated_at "
            "FROM vault_entries WHERE reference_name = ?",
            (reference_name,),
        ).fetchone()
        return dict(row) if row else None


def list_vault_entries(scope: Optional[str] = None, limit: int = 50, offset: int = 0) -> list[dict]:
    with get_db() as conn:
        query = (
            "SELECT id, scope, name, label, value_type, metadata_json, "
            "expires_at, reference_name, created_by, created_at "
            "FROM vault_entries WHERE 1=1"
        )
        params = []
        if scope:
            query += " AND scope = ?"
            params.append(scope)
        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]


def update_vault_entry(entry_id: str, **fields) -> bool:
    allowed = ("name", "label", "value_encrypted", "value_type", "metadata_json", "expires_at")
    updates = []
    params = []
    for key, val in fields.items():
        if key in allowed and val is not None:
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
            f"UPDATE vault_entries SET {', '.join(updates)} WHERE id = ?",
            params,
        )
        conn.commit()
        return cursor.rowcount > 0


def delete_vault_entry(entry_id: str) -> bool:
    with get_db() as conn:
        cursor = conn.execute("DELETE FROM vault_entries WHERE id = ?", (entry_id,))
        conn.commit()
        return cursor.rowcount > 0


def resolve_reference(reference_name: str) -> Optional[str]:
    entry = get_vault_entry_by_reference(reference_name)
    if not entry:
        return None
    if entry.get("expires_at"):
        if utc_now() > parse_utc_datetime(entry["expires_at"]):
            return None
    return decrypt_value(entry["value_encrypted"])


def mask_preview(value_encrypted: str) -> str:
    try:
        plaintext = decrypt_value(value_encrypted)
    except Exception:
        return "***"
    if len(plaintext) <= 4:
        return "***"
    return plaintext[:2] + "*" * (len(plaintext) - 4) + plaintext[-2:]


def get_vault_scopes() -> list[str]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT scope FROM vault_entries ORDER BY scope"
        ).fetchall()
        return [row["scope"] for row in rows]
