import json
import secrets
from typing import Optional
from app.database import get_db
from app.models.enums import normalize_id


def list_connector_types(include_inactive: bool = False) -> list[dict]:
    with get_db() as conn:
        query = "SELECT * FROM connector_types"
        if not include_inactive:
            query += " WHERE is_active = 1"
        query += " ORDER BY display_name"
        rows = conn.execute(query).fetchall()
        return [_row_to_connector_type(dict(row)) for row in rows]


def get_connector_type(connector_type_id: str) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM connector_types WHERE id = ?",
            (connector_type_id,),
        ).fetchone()
        return _row_to_connector_type(dict(row)) if row else None


def _row_to_connector_type(row: dict) -> dict:
    return {
        "id": row["id"],
        "display_name": row["display_name"],
        "description": row.get("description"),
        "auth_type": row["auth_type"],
        "supported_actions": json.loads(row["supported_actions_json"]),
        "required_credential_fields": json.loads(
            row["required_credential_fields_json"]
        ),
        "default_binding_rules": json.loads(row["default_binding_rules_json"])
        if row.get("default_binding_rules_json")
        else None,
        "is_active": bool(row["is_active"]),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _row_to_binding(row: dict) -> dict:
    return {
        "id": row["id"],
        "connector_type_id": row["connector_type_id"],
        "connector_display_name": row.get("connector_display_name"),
        "name": row["name"],
        "scope": row["scope"],
        "credential_id": row.get("credential_id"),
        "config_json": row.get("config_json"),
        "enabled": bool(row["enabled"]),
        "last_tested_at": row.get("last_tested_at"),
        "last_error": row.get("last_error"),
        "created_by": row.get("created_by"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def list_bindings(
    scope: Optional[str] = None,
    connector_type_id: Optional[str] = None,
    enabled: Optional[bool] = None,
) -> list[dict]:
    with get_db() as conn:
        query = "SELECT cb.*, ct.display_name as connector_display_name FROM connector_bindings cb JOIN connector_types ct ON cb.connector_type_id = ct.id WHERE 1=1"
        params = []
        if scope:
            query += " AND cb.scope = ?"
            params.append(scope)
        if connector_type_id:
            query += " AND cb.connector_type_id = ?"
            params.append(connector_type_id)
        if enabled is not None:
            query += " AND cb.enabled = ?"
            params.append(1 if enabled else 0)
        query += " ORDER BY cb.created_at DESC"
        rows = conn.execute(query, params).fetchall()
        return [_row_to_binding(dict(row)) for row in rows]


def get_binding(binding_id: str) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT cb.*, ct.display_name as connector_display_name FROM connector_bindings cb JOIN connector_types ct ON cb.connector_type_id = ct.id WHERE cb.id = ?",
            (binding_id,),
        ).fetchone()
        return _row_to_binding(dict(row)) if row else None


def get_binding_with_credential(binding_id: str) -> Optional[dict]:
    binding = get_binding(binding_id)
    if not binding:
        return None
    binding["credential_plaintext"] = None
    binding["vault_entry"] = None
    if binding.get("credential_id"):
        from app.services import vault_service

        vault_entry = vault_service.get_vault_entry(binding["credential_id"])
        if vault_entry:
            from app.services.vault_service import resolve_reference

            binding["credential_plaintext"] = resolve_reference(
                vault_entry["reference_name"]
            )
            binding["vault_entry"] = vault_entry
    return binding


def create_binding(
    connector_type_id: str,
    name: str,
    scope: str,
    credential_id: Optional[str] = None,
    config_json: Optional[str] = None,
    enabled: bool = True,
    created_by: Optional[str] = None,
) -> dict:
    normalized_scope = _normalize_scope(scope)
    binding_id = secrets.token_urlsafe(16)
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO connector_bindings
            (id, connector_type_id, name, scope, credential_id, config_json, enabled, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                binding_id,
                connector_type_id,
                name,
                normalized_scope,
                credential_id,
                config_json,
                1 if enabled else 0,
                created_by,
            ),
        )
        conn.commit()
    return get_binding(binding_id)


def update_binding(binding_id: str, **fields) -> bool:
    allowed = (
        "name",
        "scope",
        "credential_id",
        "config_json",
        "enabled",
        "last_tested_at",
        "last_error",
    )
    updates = []
    params = []
    for key, val in fields.items():
        if key in allowed and val is not None:
            if key == "enabled":
                updates.append("enabled = ?")
                params.append(1 if val else 0)
            elif key == "scope":
                updates.append("scope = ?")
                params.append(_normalize_scope(val))
            else:
                updates.append(f"{key} = ?")
                params.append(val)
    if not updates:
        return False
    updates.append("updated_at = CURRENT_TIMESTAMP")
    params.append(binding_id)
    with get_db() as conn:
        cursor = conn.execute(
            f"UPDATE connector_bindings SET {', '.join(updates)} WHERE id = ?",
            params,
        )
        conn.commit()
        return cursor.rowcount > 0


def delete_binding(binding_id: str) -> bool:
    with get_db() as conn:
        cursor = conn.execute(
            "DELETE FROM connector_bindings WHERE id = ?", (binding_id,)
        )
        conn.commit()
        return cursor.rowcount > 0


def test_binding(binding_id: str) -> dict:
    binding = get_binding_with_credential(binding_id)
    if not binding:
        return {"success": False, "error": "Binding not found"}
    if not binding.get("enabled"):
        return {"success": False, "error": "Binding is disabled"}

    connector_type = get_connector_type(binding["connector_type_id"])
    if not connector_type:
        return {"success": False, "error": "Connector type not found"}

    credential = binding.get("credential_plaintext")
    if not credential:
        return {"success": False, "error": "No credential linked to this binding"}

    try:
        from app.connectors import get_connector

        connector = get_connector(connector_type["id"])
        result = connector.test_connection(credential, binding.get("config_json"))
        if result.get("success"):
            update_binding(binding_id, last_tested_at=_utc_now(), last_error=None)
        else:
            update_binding(
                binding_id, last_tested_at=_utc_now(), last_error=result.get("error")
            )
        return result
    except Exception as e:
        update_binding(binding_id, last_tested_at=_utc_now(), last_error=str(e))
        return {"success": False, "error": str(e)}


def log_execution(
    binding_id: str,
    action: str,
    params_json: Optional[str],
    result_status: str,
    result_body_json: Optional[str] = None,
    error_message: Optional[str] = None,
    duration_ms: Optional[int] = None,
) -> str:
    execution_id = secrets.token_urlsafe(16)
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO connector_executions
            (id, binding_id, action, params_json, result_status, result_body_json, error_message, duration_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                execution_id,
                binding_id,
                action,
                params_json,
                result_status,
                result_body_json,
                error_message,
                duration_ms,
            ),
        )
        conn.commit()
    return execution_id


def list_executions(binding_id: str, limit: int = 50, offset: int = 0) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM connector_executions WHERE binding_id = ? ORDER BY executed_at DESC LIMIT ? OFFSET ?",
            (binding_id, limit, offset),
        ).fetchall()
        return [dict(row) for row in rows]


def _normalize_scope(scope: str) -> str:
    parts = scope.split(":", 1)
    if len(parts) == 2 and parts[0].lower() in ("user", "agent", "workspace", "shared"):
        return f"{parts[0].lower()}:{normalize_id(parts[1])}"
    return scope


def _utc_now() -> str:
    from app.time_utils import utc_now

    return utc_now()
