import json
from typing import Any, Optional

from app.database import get_db


ACTOR_TYPES = ("user", "agent", "broker", "system")
RESULT_TYPES = ("success", "failure", "blocked")

AUDIT_ACTIONS = (
    "session_login",
    "session_logout",
    "user_registered",
    "agent_created",
    "agent_updated",
    "agent_key_rotated",
    "agent_deactivated",
    "agent_reactivated",
    "agent_purged",
    "credential_entry_created",
    "credential_entry_updated",
    "credential_entry_deleted",
    "credential_reference",
    "credential_reveal",
    "credential_key_rotated",
    "credential_key_restored",
    "broker_resolve",
    "credential_delete",
    "memory_write",
    "memory_import",
    "memory_search",
    "memory_retract",
    "memory_restore",
    "memory_delete",
    "retrieval_degraded",
    "activity_update",
    "activity_pickup",
    "activity_heartbeat",
    "activity_ambiguous_update",
    "activity_recovery",
    "activity_reassigned",
    "activity_cancelled",
    "activity_resumed",
    "activity_pruned",
    "briefing_generated",
    "handoff_created",
    "backup_export",
    "backup_restore",
    "scope_grant",
    "scope_denied",
    "workspace_deactivated",
    "workspace_reactivated",
    "workspace_purged",
    "workspace_created",
    "workspace_updated",
    "workspace_collaborator_upserted",
    "workspace_collaborator_removed",
    "audit_pruned",
    "scratchpad_pruned",
    "memory_ttl_swept",
    "password_change",
    "otp_enrolled",
    "otp_disabled",
    "user_created",
    "user_updated",
    "user_deleted",
    "setup_verification",
    "system_setting_updated",
    "connector_binding_created",
    "connector_binding_updated",
    "connector_binding_deleted",
    "connector_binding_tested",
    "connector_type_imported",
    "connector_type_refreshed",
    "connector_type_actions_updated",
    "connector_type_deleted",
    "connector_action_executed",
    "webhook_created",
    "webhook_updated",
    "webhook_deleted",
    "webhook_test_delivery",
    "inbound_key_generated",
    "inbound_key_rotated",
    "inbound_webhook_received",
    "inbound_webhook_rejected",
    "inbound_webhook_note",
)


def write_event(
    actor_type: str,
    actor_id: Optional[str],
    action: str,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    result: str = "success",
    details: Optional[dict[str, Any]] = None,
    ip_address: Optional[str] = None,
) -> int:
    if actor_type not in ACTOR_TYPES:
        raise ValueError(f"Invalid actor_type: {actor_type}")
    if result not in RESULT_TYPES:
        raise ValueError(f"Invalid result: {result}")
    if action not in AUDIT_ACTIONS:
        raise ValueError(f"Invalid action: {action}")

    details_json = None
    if details:
        details_json = json.dumps(_sanitize_details(details))

    with get_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO audit_log
            (actor_type, actor_id, action, resource_type, resource_id, result, details_json, ip_address)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                actor_type,
                actor_id,
                action,
                resource_type,
                resource_id,
                result,
                details_json,
                ip_address,
            ),
        )
        return cursor.lastrowid


SECRET_KEYWORDS = (
    "password",
    "secret",
    "token",
    "key",
    "credential",
    "value",
    "api_key",
)


def _is_secret_key(key: str) -> bool:
    lower_key = key.lower()
    return any(kw in lower_key for kw in SECRET_KEYWORDS)


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, dict):
        return _sanitize_details(value)
    elif isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    return value


def _sanitize_details(details: dict[str, Any]) -> dict[str, Any]:
    sanitized = {}
    for key, value in details.items():
        if _is_secret_key(key):
            sanitized[key] = "[REDACTED]"
        else:
            sanitized[key] = _sanitize_value(value)
    return sanitized


def count_events(
    actor_type: Optional[str] = None,
    actor_id: Optional[str] = None,
    action: Optional[str] = None,
    resource_type: Optional[str] = None,
    result: Optional[str] = None,
) -> int:
    conditions = []
    params = []
    if actor_type:
        conditions.append("actor_type = ?")
        params.append(actor_type)
    if actor_id:
        conditions.append("actor_id = ?")
        params.append(actor_id)
    if action:
        conditions.append("action = ?")
        params.append(action)
    if resource_type:
        conditions.append("resource_type = ?")
        params.append(resource_type)
    if result:
        conditions.append("result = ?")
        params.append(result)

    where_clause = " AND ".join(conditions) if conditions else "1=1"
    with get_db() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) FROM audit_log WHERE {where_clause}", params
        ).fetchone()
        return row[0] if row else 0


def query_events(
    actor_type: Optional[str] = None,
    actor_id: Optional[str] = None,
    action: Optional[str] = None,
    resource_type: Optional[str] = None,
    result: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    conditions = []
    params = []

    if actor_type:
        conditions.append("actor_type = ?")
        params.append(actor_type)
    if actor_id:
        conditions.append("actor_id = ?")
        params.append(actor_id)
    if action:
        conditions.append("action = ?")
        params.append(action)
    if resource_type:
        conditions.append("resource_type = ?")
        params.append(resource_type)
    if result:
        conditions.append("result = ?")
        params.append(result)

    where_clause = " AND ".join(conditions) if conditions else "1=1"

    with get_db() as conn:
        cursor = conn.execute(
            f"""
            SELECT id, timestamp, actor_type, actor_id, action, resource_type,
                   resource_id, result, details_json, ip_address
            FROM audit_log
            WHERE {where_clause}
            ORDER BY timestamp DESC
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        )
        rows = cursor.fetchall()
        return [dict(row) for row in rows]
