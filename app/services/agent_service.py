import secrets
import hashlib
import json
from typing import Optional

from app.database import get_db
from app.models.enums import normalize_id, SCOPE_PREFIXES
from app.security.scope_utils import normalize_scope_string
from app.config import settings
from app.services import cleanup_service


def _normalize_scopes(scopes: list[str]) -> list[str]:
    normalized = []
    for scope in scopes:
        normalized_scope = normalize_scope_string(scope)
        if normalized_scope not in normalized:
            normalized.append(normalized_scope)
    return normalized


def _with_own_scope(agent_id: str, scopes: list[str]) -> list[str]:
    own_scope = f"agent:{normalize_id(agent_id)}"
    normalized = _normalize_scopes(scopes)
    if own_scope not in normalized:
        normalized.insert(0, own_scope)
    return normalized


def generate_api_key() -> tuple[str, str]:
    random_bytes = secrets.token_bytes(32)
    plaintext = f"ac_sk_{secrets.token_urlsafe(32)}"
    key_hash = hashlib.sha256(plaintext.encode()).hexdigest()
    return plaintext, key_hash


def verify_api_key(plaintext: str, key_hash: str) -> bool:
    expected = hashlib.sha256(plaintext.encode()).hexdigest()
    return secrets.compare_digest(expected, key_hash)


def is_solo_mode_enabled() -> bool:
    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM system_settings WHERE key = 'solo_mode_enabled'"
        ).fetchone()
    if not row:
        return True
    return str(row["value"]).strip().lower() in ("1", "true", "yes", "on")


def create_agent(
    agent_id: str,
    display_name: str,
    owner_user_id: str,
    description: str = "",
    default_user_id: Optional[str] = None,
    read_scopes: Optional[list[str]] = None,
    write_scopes: Optional[list[str]] = None,
) -> tuple[dict, str]:
    normalized_id = normalize_id(agent_id)
    api_key_plaintext, api_key_hash = generate_api_key()

    if default_user_id is None:
        default_user_id = owner_user_id

    if read_scopes is None:
        read_scopes = [f"agent:{normalized_id}", "shared"]
        if is_solo_mode_enabled():
            read_scopes.insert(1, f"user:{owner_user_id}")
    else:
        read_scopes = _with_own_scope(normalized_id, read_scopes)

    if write_scopes is None:
        write_scopes = [f"agent:{normalized_id}"]
        if settings.shared_scope_agent_list and normalized_id in settings.shared_scope_agent_list:
            write_scopes.append("shared")
    else:
        write_scopes = _with_own_scope(normalized_id, write_scopes)

    read_scopes_json = json.dumps(read_scopes)
    write_scopes_json = json.dumps(write_scopes)

    with get_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO agents (id, display_name, description, owner_user_id, default_user_id,
                               read_scopes_json, write_scopes_json, api_key_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (normalized_id, display_name, description, owner_user_id, default_user_id,
             read_scopes_json, write_scopes_json, api_key_hash),
        )
        conn.commit()

        agent = {
            "id": normalized_id,
            "display_name": display_name,
            "description": description,
            "owner_user_id": owner_user_id,
            "default_user_id": default_user_id,
            "read_scopes_json": read_scopes_json,
            "write_scopes_json": write_scopes_json,
            "is_active": True,
        }

    return agent, api_key_plaintext


def get_agent_by_id(agent_id: str) -> Optional[dict]:
    with get_db() as conn:
        cursor = conn.execute(
            """
            SELECT id, display_name, description, owner_user_id, default_user_id,
                   read_scopes_json, write_scopes_json, api_key_hash, is_active, created_at
            FROM agents WHERE id = ?
            """,
            (agent_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def get_agent_by_api_key(plaintext_key: str) -> Optional[dict]:
    key_hash = hashlib.sha256(plaintext_key.encode()).hexdigest()
    with get_db() as conn:
        cursor = conn.execute(
            """
            SELECT id, display_name, description, owner_user_id, default_user_id,
                   read_scopes_json, write_scopes_json, api_key_hash, is_active, created_at
            FROM agents WHERE api_key_hash = ?
            """,
            (key_hash,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def list_agents(owner_user_id: Optional[str] = None, is_active: Optional[bool] = None) -> list[dict]:
    with get_db() as conn:
        query = "SELECT id, display_name, description, owner_user_id, default_user_id, read_scopes_json, write_scopes_json, is_active, created_at FROM agents WHERE 1=1"
        params = []

        if owner_user_id:
            query += " AND owner_user_id = ?"
            params.append(owner_user_id)
        if is_active is not None:
            query += " AND is_active = ?"
            params.append(is_active)

        query += " ORDER BY created_at DESC"
        cursor = conn.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]


def update_agent(
    agent_id: str,
    display_name: Optional[str] = None,
    description: Optional[str] = None,
    read_scopes: Optional[list[str]] = None,
    write_scopes: Optional[list[str]] = None,
) -> bool:
    updates = []
    params = []

    if display_name is not None:
        updates.append("display_name = ?")
        params.append(display_name)
    if description is not None:
        updates.append("description = ?")
        params.append(description)
    if read_scopes is not None:
        updates.append("read_scopes_json = ?")
        params.append(json.dumps(_with_own_scope(agent_id, read_scopes)))
    if write_scopes is not None:
        updates.append("write_scopes_json = ?")
        params.append(json.dumps(_with_own_scope(agent_id, write_scopes)))

    if not updates:
        return False

    updates.append("updated_at = CURRENT_TIMESTAMP")
    params.append(agent_id)

    with get_db() as conn:
        cursor = conn.execute(
            f"UPDATE agents SET {', '.join(updates)} WHERE id = ?",
            params,
        )
        conn.commit()
        return cursor.rowcount > 0


def deactivate_agent(agent_id: str) -> bool:
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE agents SET is_active = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (agent_id,),
        )
        conn.commit()
        return cursor.rowcount > 0


def reactivate_agent(agent_id: str) -> bool:
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE agents SET is_active = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (agent_id,),
        )
        conn.commit()
        return cursor.rowcount > 0


def rotate_agent_key(agent_id: str) -> Optional[str]:
    new_plaintext, new_hash = generate_api_key()
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE agents SET api_key_hash = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND is_active = 1",
            (new_hash, agent_id),
        )
        conn.commit()
        if cursor.rowcount == 0:
            return None
    return new_plaintext


def delete_agent_hard(agent_id: str) -> bool:
    normalized_id = normalize_id(agent_id)
    scope = f"agent:{normalized_id}"
    with get_db() as conn:
        cleanup_service.delete_scope_data(conn, scope)
        conn.execute(
            """
            DELETE FROM agent_activity
            WHERE agent_id = ? OR assigned_agent_id = ? OR reassigned_from_agent_id = ?
            """,
            (normalized_id, normalized_id, normalized_id),
        )
        cursor = conn.execute("DELETE FROM agents WHERE id = ?", (normalized_id,))
        conn.commit()
        return cursor.rowcount > 0


def parse_scopes(scopes_json: str) -> list[str]:
    try:
        raw = json.loads(scopes_json)
        return _normalize_scopes(raw) if isinstance(raw, list) else []
    except (json.JSONDecodeError, TypeError):
        return []
